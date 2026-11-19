"""The recovery ladder: a pure, synchronous, deterministic state machine.

``RecoveryLadder.step(assessment) -> Decision`` is called once per assessment.
``RecoveryLadder.on_result(result)`` reports what the safety gate did with the last command.
The ladder never talks to the printer; it only proposes commands. The gate decides.

Levels (built from ``ServiceConfig``)::

    0       normal
    1       warn            Notify with minutes_to_defect
    2..     speed steps     SetSpeedFactor(p) for p in ladder.speed_steps_percent
    ..      heat steps      SetNozzleTemp(nominal + k * temp_step_c) while <= temp_ceiling_c
    ..      purge           RunPurge (only if purge_enabled)
    last    pause           Pause

States (``LadderState``):

* ``warmup``          - t < detector.warmup_s; no decisions at all.
* ``normal``          - level 0. p_clog >= warn_threshold -> Notify (warn), go to ``warned``.
* ``warned``          - level 1. p_clog >= act_threshold -> climb to the first correction.
  p_clog below watch_threshold for verify_window_s -> back to ``normal`` (no command).
* ``awaiting_result`` - a printer command was proposed; waiting for ``on_result``.
* ``verifying``       - a command was applied at time ta; window W = max(verify_window_s,
  min_action_interval_s). Verified recovery: p_clog has stayed below watch_threshold for
  verify_window_s and t - ta >= W -> ``Recovery(verified=True)`` and step down one level.
  Not recovered at the end of the window (p_clog >= watch_threshold once t - ta >= W) -> climb.
  The same window rule applies after a step-down, so a rising p_clog re-climbs.
* ``degraded``        - a command was sent but not acknowledged. It is treated as not applied
  and the ladder may only escalate to Pause (when p_clog >= act_threshold, or >= watch at the
  end of the window). No step-downs either; the user is told.
* ``pause_pending``   - a Pause was not acknowledged; it is retried on every step.
* ``paused``          - terminal for this print.

Step-down order: temperature goes straight back to nominal first, then speed returns one step
at a time towards 100%, then ``normal``.

Heat-creep hint: if the window after a temperature raise ends with severity higher than it was
when the raise was decided (by more than ``HEAT_CREEP_MARGIN``), the restriction is probably
heat creep (heat makes it worse). The ladder reverts the temperature to nominal and skips every
further heat step for the rest of the print.

Unknown or stale assessments (state 'unknown', data_fresh False, p_clog missing) -> hold.
``recoverable is False`` with p_clog >= warn_threshold and ``pause_if_unrecoverable`` -> Pause.
Intervention cap: when a further correction would exceed ``max_interventions_per_print``,
Pause and then a critical Notify. A *correction* is an escalation (lower speed, higher
temperature, purge); restores are not counted. The gate counts the same way.

Watch mode: the ladder runs on paper. Every printer command it would send is replaced by a
Notify whose message and the Decision ``reason`` name the would-have-done action; the paper
state advances as if the action had been applied. ``first_warn_t_s`` records the first warning
in either mode (the runtime writes it to ``meta.alert_time_s``).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from clogguard.core.commands import (
    PRINTER_KINDS,
    Command,
    Notify,
    Pause,
    RunPurge,
    SetNozzleTemp,
    SetSpeedFactor,
)
from clogguard.core.config import ServiceConfig
from clogguard.core.events import Assessment, CommandResult, Decision, Lifecycle, Recovery

# ASSUMPTION: a severity rise of more than 0.05 (on the 0..1 scale) over a verify window after a
# temperature raise is treated as the heat-creep signature. Tune on recovery-tuning runs.
HEAT_CREEP_MARGIN = 0.05

LevelKind = Literal["normal", "warn", "speed", "temp", "purge", "pause"]
Move = Literal["climb", "temp_raise", "down", "heat_revert", "pause"]


class LadderState(StrEnum):
    WARMUP = "warmup"
    NORMAL = "normal"
    WARNED = "warned"
    AWAITING = "awaiting_result"
    VERIFYING = "verifying"
    DEGRADED = "degraded"
    PAUSE_PENDING = "pause_pending"
    PAUSED = "paused"


@dataclass(frozen=True)
class Level:
    index: int
    kind: LevelKind
    value: float | None = None


@dataclass(frozen=True)
class _Pending:
    command: Command
    move: Move
    level: int
    speed: int
    temp: float
    corrective: bool
    t: float
    severity: float | None
    prev_state: LadderState


def describe(cmd: Command) -> str:
    """Human-readable form of a command for reasons and messages."""
    if isinstance(cmd, SetSpeedFactor):
        return f"set speed factor {cmd.percent}%"
    if isinstance(cmd, SetNozzleTemp):
        return f"set nozzle temperature {cmd.target_c:.1f} C"
    if isinstance(cmd, RunPurge):
        return "run purge macro"
    if isinstance(cmd, Pause):
        return "pause the print"
    return f"notify ({cmd.level})"


def build_levels(config: ServiceConfig) -> list[Level]:
    lad = config.ladder
    kinds: list[tuple[LevelKind, float | None]] = [("normal", None), ("warn", None)]
    kinds += [("speed", float(p)) for p in lad.speed_steps_percent]
    nominal, ceiling = config.material.nominal_temp_c, config.temp_ceiling_c
    k = 1
    while (v := round(nominal + k * lad.temp_step_c, 1)) <= ceiling:
        kinds.append(("temp", v))
        k += 1
    if lad.purge_enabled:
        kinds.append(("purge", None))
    kinds.append(("pause", None))
    return [Level(i, kind, val) for i, (kind, val) in enumerate(kinds)]


class RecoveryLadder:
    def __init__(self, config: ServiceConfig) -> None:
        self.cfg = config
        self.levels = build_levels(config)
        self.watch_mode = config.mode != "act"
        det, lad = config.detector, config.ladder
        self.window_s = max(lad.verify_window_s, lad.min_action_interval_s)
        self._nominal = config.material.nominal_temp_c
        self._det = det
        self._lad = lad

        self.state = LadderState.WARMUP
        self.level = 0
        self.speed = 100
        self.temp = self._nominal
        self.heat_disabled = False
        self.interventions = 0
        self.first_warn_t_s: float | None = None

        self._last_action_t: float | None = None
        self._retry_after = -math.inf
        self._action_t = 0.0
        self._below_since: float | None = None
        self._last_move: Move | None = None
        self._sev_at_raise: float | None = None
        self._awaiting: _Pending | None = None
        self._queue: deque[tuple[Command, str]] = deque()
        self._events: list[Recovery | Lifecycle] = []

    # -- public --------------------------------------------------------------------------
    def drain_events(self) -> list[Recovery | Lifecycle]:
        """Side events (verified recoveries, cap reached) since the last call."""
        out, self._events = self._events, []
        return out

    def take_queued(self, t_s: float, wall_time: datetime) -> Decision | None:
        """A queued follow-up decision (e.g. the critical Notify after a cap Pause)."""
        if not self._queue:
            return None
        cmd, reason = self._queue.popleft()
        return Decision(
            t_s=t_s, wall_time=wall_time, ladder_level=self.level, command=cmd, reason=reason
        )

    def step(self, a: Assessment) -> Decision:
        queued = self.take_queued(a.t_s, a.wall_time)
        if queued is not None:
            return queued
        if self.state is LadderState.PAUSED:
            return self._hold(a, "paused; no further actions this print")
        if self.state is LadderState.PAUSE_PENDING:
            return self._pause(a, "retrying unacknowledged pause")
        if self.state is LadderState.AWAITING:
            return self._hold(a, "awaiting result of previous command")
        if a.t_s < self._det.warmup_s:
            return self._hold(a, "warmup")
        if self.state is LadderState.WARMUP:
            self.state = LadderState.NORMAL
        p = a.p_clog
        if not a.data_fresh or a.state == "unknown" or p is None:
            self._below_since = None
            return self._hold(a, "data unknown or stale")
        det = self._det
        if a.recoverable is False and self._lad.pause_if_unrecoverable and p >= det.warn_threshold:
            return self._pause(a, "clog assessed as unrecoverable")
        if p < det.watch_threshold:
            if self._below_since is None:
                self._below_since = a.t_s
        else:
            self._below_since = None

        if self.state is LadderState.NORMAL:
            if p >= det.warn_threshold:
                return self._warn(a)
            return self._hold(a, "normal")
        if self.state is LadderState.WARNED:
            if p >= det.act_threshold:
                return self._climb(a)
            if self._recovered_for_window(a):
                self.state, self.level = LadderState.NORMAL, 0
                return self._hold(a, "warning cleared")
            return self._hold(a, "warned; watching")
        window_done = a.t_s - self._action_t >= self.window_s
        if self.state is LadderState.DEGRADED:
            if p >= det.act_threshold or (window_done and p >= det.watch_threshold):
                return self._pause(a, "previous command not acknowledged and clog persists")
            return self._hold(a, "corrections suspended after unacknowledged command")
        # VERIFYING
        if window_done and self._recovered_for_window(a):
            return self._step_down(a)
        if window_done and p >= det.watch_threshold:
            if self._heat_creep(a):
                return self._heat_revert(a)
            return self._climb(a)
        return self._hold(a, f"verifying level {self.level}")

    def on_result(self, r: CommandResult) -> None:
        pend = self._awaiting
        if pend is None or r.command.kind == "notify":
            return
        self._awaiting = None
        if r.accepted and r.acked_at_s is not None:
            self._apply(pend)
        elif pend.move == "pause":
            self.state = LadderState.PAUSE_PENDING
            self._queue.append(
                (Notify(level="critical", message="Pause not acknowledged; retrying."),
                 "pause not acknowledged")
            )
        elif not r.accepted:
            self.state = pend.prev_state
            self._retry_after = pend.t + self._lad.min_action_interval_s
        else:
            # Sent but not acknowledged: not applied; only Pause from here on.
            self.state = LadderState.DEGRADED
            self._action_t = pend.t
            self._last_action_t = pend.t
            msg = f"Command not acknowledged ({describe(pend.command)}); corrections suspended."
            self._queue.append((Notify(level="critical", message=msg), "command not acknowledged"))

    # -- transitions ---------------------------------------------------------------------
    def _recovered_for_window(self, a: Assessment) -> bool:
        return (
            self._below_since is not None
            and a.t_s - self._below_since >= self._lad.verify_window_s
        )

    def _heat_creep(self, a: Assessment) -> bool:
        return (
            self._last_move == "temp_raise"
            and a.severity is not None
            and self._sev_at_raise is not None
            and a.severity > self._sev_at_raise + HEAT_CREEP_MARGIN
        )

    def _can_act(self, t: float) -> bool:
        if t < self._retry_after:
            return False
        last = self._last_action_t
        return last is None or t - last >= self._lad.min_action_interval_s

    def _speed_level(self, percent: int) -> int:
        for lv in self.levels:
            if lv.kind == "speed" and lv.value == percent:
                return lv.index
        return 0

    def _warn(self, a: Assessment) -> Decision:
        self.state, self.level = LadderState.WARNED, 1
        if self.first_warn_t_s is None:
            self.first_warn_t_s = a.t_s
        mins = a.minutes_to_defect
        eta = f"about {mins:.0f} min to visible defect" if mins is not None else "time unknown"
        msg = f"Possible nozzle clog developing (p_clog={a.p_clog:.2f}); {eta}."
        return Decision(
            t_s=a.t_s, wall_time=a.wall_time, ladder_level=1,
            command=Notify(level="warning", message=msg), reason="warn: p_clog >= warn_threshold",
        )

    def _climb(self, a: Assessment) -> Decision:
        nxt = self.level + 1
        while self.heat_disabled and self.levels[nxt].kind == "temp":
            nxt += 1
        lv = self.levels[nxt]
        if lv.kind == "pause":
            return self._pause(a, "top of ladder reached without recovery")
        if lv.kind == "warn":
            return self._warn(a)
        if self.interventions >= self._lad.max_interventions_per_print:
            self._events.append(
                Lifecycle(t_s=a.t_s, wall_time=a.wall_time, what="intervention_cap_reached",
                          detail=f"{self.interventions} corrections")
            )
            msg = "Intervention cap reached; print paused. Check the nozzle."
            self._queue.append((Notify(level="critical", message=msg), "intervention cap"))
            return self._pause(a, "intervention cap reached")
        if not self._can_act(a.t_s):
            return self._hold(a, "climb due but waiting for min action interval")
        speed, temp = self.speed, self.temp
        move: Move = "climb"
        cmd: Command
        if lv.kind == "speed":
            speed = int(lv.value or 100)
            cmd = SetSpeedFactor(percent=speed)
        elif lv.kind == "temp":
            temp = float(lv.value or self._nominal)
            cmd = SetNozzleTemp(target_c=temp)
            move = "temp_raise"
        else:
            cmd = RunPurge()
        pend = _Pending(cmd, move, nxt, speed, temp, True, a.t_s, a.severity, self.state)
        return self._propose(a, pend, f"climb to level {nxt} ({lv.kind}): not recovered")

    def _step_down(self, a: Assessment) -> Decision:
        if not self._can_act(a.t_s):
            return self._hold(a, "recovered; waiting to step down")
        detail = f"verified at level {self.level}"
        if self.temp > self._nominal:
            cmd: Command = SetNozzleTemp(target_c=self._nominal)
            pend = _Pending(cmd, "down", self._speed_level(self.speed), self.speed,
                            self._nominal, False, a.t_s, a.severity, self.state)
        elif self.speed < 100:
            higher = [p for p in self._lad.speed_steps_percent if p > self.speed]
            nxt = min(higher) if higher else 100
            pend = _Pending(SetSpeedFactor(percent=nxt), "down", self._speed_level(nxt), nxt,
                            self.temp, False, a.t_s, a.severity, self.state)
        else:
            self._events.append(Recovery(t_s=a.t_s, wall_time=a.wall_time, verified=True,
                                         ladder_level=self.level, detail=detail + "; normal"))
            self.state, self.level = LadderState.NORMAL, 0
            return self._hold(a, "recovered; back to normal")
        self._events.append(Recovery(t_s=a.t_s, wall_time=a.wall_time, verified=True,
                                     ladder_level=self.level, detail=detail + "; stepping down"))
        return self._propose(a, pend, f"step down to level {pend.level}: verified recovery")

    def _heat_revert(self, a: Assessment) -> Decision:
        self.heat_disabled = True
        pend = _Pending(SetNozzleTemp(target_c=self._nominal), "heat_revert",
                        self._speed_level(self.speed), self.speed, self._nominal, False, a.t_s,
                        a.severity, self.state)
        return self._propose(
            a, pend, "heat-creep hint: severity rose after temperature raise; reverting "
            "temperature to nominal and skipping heat steps this print",
        )

    def _pause(self, a: Assessment, reason: str) -> Decision:
        pend = _Pending(Pause(reason=reason[:200]), "pause", len(self.levels) - 1, self.speed,
                        self.temp, False, a.t_s, a.severity, self.state)
        return self._propose(a, pend, f"pause: {reason}")

    def _propose(self, a: Assessment, pend: _Pending, reason: str) -> Decision:
        cmd = pend.command
        if self.watch_mode:
            self._apply(pend)
            level = "critical" if cmd.kind == "pause" else "warning"
            text = f"[watch mode] would {describe(cmd)}: {reason}"
            note = Notify(level=level, message=text[:500])
            return Decision(t_s=a.t_s, wall_time=a.wall_time, ladder_level=pend.level,
                            command=note, reason=f"watch mode: would {describe(cmd)}; {reason}")
        assert cmd.kind in PRINTER_KINDS
        self._awaiting = pend
        self.state = LadderState.AWAITING
        return Decision(t_s=a.t_s, wall_time=a.wall_time, ladder_level=pend.level, command=cmd,
                        reason=reason)

    def _apply(self, pend: _Pending) -> None:
        self.level, self.speed, self.temp = pend.level, pend.speed, pend.temp
        if pend.corrective:
            self.interventions += 1
        self._last_action_t = pend.t
        self._action_t = pend.t
        self._below_since = None
        self._last_move = pend.move
        self._sev_at_raise = pend.severity if pend.move == "temp_raise" else self._sev_at_raise
        self.state = LadderState.PAUSED if pend.move == "pause" else LadderState.VERIFYING

    def _hold(self, a: Assessment, reason: str) -> Decision:
        return Decision(t_s=a.t_s, wall_time=a.wall_time, ladder_level=self.level, command=None,
                        reason=reason)
