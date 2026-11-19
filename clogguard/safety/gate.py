"""The safety gate: the only code that turns a ``Command`` into a printer RPC.

Every submission returns a ``CommandResult``. The gate rejects; it never adjusts a command.
See ``clogguard/safety/README.md`` for the full list of guarantees and how each is tested.

Outcome semantics (``CommandResult``):

* ``accepted=False`` - rejected by the gate. Nothing was sent. ``rejected_reason`` starts with
  a stable code (``REJECT_*`` below) followed by ``": "`` and detail.
* ``accepted=True, acked_at_s`` set - sent and acknowledged by the printer (or notifier).
* ``accepted=True, acked_at_s=None`` - sent but not acknowledged: the transport raised or no
  ack arrived within ``command_ack_timeout_s``. ``rejected_reason`` then holds a delivery note
  starting with ``NOT_ACKED`` (the core ``CommandResult`` has no separate error field). The
  command may or may not have been applied; callers must treat it as **not applied** for
  recovery purposes (``was_applied`` is False) while the gate counts it, conservatively, as
  sent for the min-interval and intervention-cap limits.

Time: ``sent_at_s`` is ``now_s`` (print time at submission). ``acked_at_s`` is ``sent_at_s`` plus
the elapsed seconds on the gate's monotonic clock between send and ack.
"""

from __future__ import annotations

import asyncio
import math
import time
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from clogguard.core.commands import (
    ALLOWED_KINDS,
    PRINTER_KINDS,
    Command,
    Notify,
    Pause,
    RunPurge,
    SetNozzleTemp,
    SetSpeedFactor,
    parse_command,
)
from clogguard.core.config import ServiceConfig
from clogguard.core.events import CommandResult
from clogguard.safety.protocols import Notifier, PrinterLink
from clogguard.safety.templates import (
    RpcCall,
    format_temp,
    is_safe_macro,
    nozzle_temp_rpc,
    pause_rpc,
    purge_rpc,
    speed_factor_rpc,
)

REJECT_INVALID = "invalid_command"
REJECT_KIND = "kind_not_allowed"
REJECT_NON_FINITE = "non_finite"
REJECT_WATCH = "watch_mode"
REJECT_STALE = "data_stale"
REJECT_TEMP_HIGH = "temp_above_ceiling"
REJECT_TEMP_LOW = "temp_below_nominal"
REJECT_SPEED = "speed_not_allowed"
REJECT_PURGE = "purge_disabled"
REJECT_INTERVAL = "min_interval"
REJECT_CAP = "intervention_cap"
NOT_ACKED = "not_acked"

NOTIFIER_TARGET = "notifier"


def was_applied(result: CommandResult) -> bool:
    """True only when the gate accepted the command and the printer acknowledged it."""
    return result.accepted and result.acked_at_s is not None


def _placeholder(obj: object) -> Notify:
    """Stand-in recorded in a CommandResult when the submitted object is not a valid Command
    (a CommandResult must carry a valid Command so the audit log round-trips)."""
    return Notify(level="critical", message=f"[invalid command placeholder] {obj!r}"[:500])


class SafetyGate:
    """Validates commands against the live config and sends accepted ones.

    State per print: time of the last accepted printer command (Pause excluded), number of
    accepted corrective commands, and the speed/temperature the gate last sent. A *corrective*
    command is an escalation relative to that state: a speed below the last sent speed, a
    temperature above the last sent target, or a purge. Restores (towards 100% or towards
    nominal) are not corrective, so a verified recovery can always step back down.
    """

    def __init__(
        self,
        config: ServiceConfig,
        link: PrinterLink,
        notifier: Notifier,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg = config
        self._link = link
        self._notifier = notifier
        self._clock = clock
        self._wall = wall_clock
        self.reset_for_new_print()

    # -- state ---------------------------------------------------------------------------
    def reset_for_new_print(self) -> None:
        self.last_action_s: float | None = None
        self.interventions = 0
        self.speed_percent = 100
        self.temp_target_c = self._cfg.material.nominal_temp_c

    # -- main entry ----------------------------------------------------------------------
    async def submit(
        self, cmd: object, *, now_s: float, decision_t_s: float | None, data_fresh: bool
    ) -> CommandResult:
        def result(command: Command, **kw: Any) -> CommandResult:
            return CommandResult(
                t_s=now_s, wall_time=self._wall(), command=command, decision_t_s=decision_t_s,
                **kw,
            )

        def reject(command: Command, code: str, detail: str) -> CommandResult:
            return result(command, accepted=False, rejected_reason=f"{code}: {detail}")

        # 1. Re-validate from plain data, never trusting the object.
        data: object = cmd
        if isinstance(cmd, BaseModel):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # serializer warnings on unvalidated objects
                    data = cmd.model_dump()
            except Exception as exc:
                return reject(_placeholder(cmd), REJECT_INVALID, f"dump failed: {exc!r}"[:200])
        if not isinstance(data, dict):
            return reject(_placeholder(cmd), REJECT_INVALID, "not a command object")
        kind = data.get("kind")
        if kind not in ALLOWED_KINDS:
            return reject(_placeholder(cmd), REJECT_KIND, f"kind {kind!r}")
        if any(isinstance(v, float) and not math.isfinite(v) for v in data.values()):
            return reject(_placeholder(cmd), REJECT_NON_FINITE, "non-finite field")
        try:
            parsed = parse_command(data)
        except Exception as exc:  # pydantic.ValidationError or anything odd in the data
            return reject(_placeholder(cmd), REJECT_INVALID, type(exc).__name__)

        # 2. Notify: never the printer.
        if isinstance(parsed, Notify):
            return await self._deliver_notify(parsed, now_s, result)

        assert parsed.kind in PRINTER_KINDS
        if self._cfg.mode != "act":
            return reject(parsed, REJECT_WATCH, "watch mode never sends printer commands")

        # 3. Pause: always allowed in act mode.
        if isinstance(parsed, Pause):
            return await self._send(parsed, pause_rpc(), now_s, result)

        # 4. Corrective commands.
        if data_fresh is not True or not math.isfinite(now_s):
            return reject(parsed, REJECT_STALE, "data not fresh")
        rpc, corrective, problem = self._check_value(parsed)
        if problem is not None:
            return reject(parsed, *problem)
        assert rpc is not None
        lim = self._cfg.ladder
        last = self.last_action_s
        if last is not None and now_s - last < lim.min_action_interval_s:
            return reject(
                parsed, REJECT_INTERVAL,
                f"{now_s - last:.1f}s since last action < {lim.min_action_interval_s}s",
            )
        if corrective and self.interventions >= lim.max_interventions_per_print:
            return reject(parsed, REJECT_CAP, f"{self.interventions} corrective commands sent")

        # Accepted: record state before the first await, so concurrent submissions see it.
        self.last_action_s = now_s
        if corrective:
            self.interventions += 1
        if isinstance(parsed, SetSpeedFactor):
            self.speed_percent = parsed.percent
        elif isinstance(parsed, SetNozzleTemp):
            self.temp_target_c = float(format_temp(parsed.target_c))
        return await self._send(parsed, rpc, now_s, result)

    # -- helpers -------------------------------------------------------------------------
    def _check_value(
        self, cmd: SetSpeedFactor | SetNozzleTemp | RunPurge
    ) -> tuple[RpcCall | None, bool, tuple[str, str] | None]:
        cfg = self._cfg
        if isinstance(cmd, SetNozzleTemp):
            sent = float(format_temp(cmd.target_c))  # the value that would go on the wire
            ceiling = cfg.temp_ceiling_c
            nominal = cfg.material.nominal_temp_c
            if cmd.target_c > ceiling or sent > ceiling:
                return None, False, (REJECT_TEMP_HIGH, f"{cmd.target_c} > ceiling {ceiling}")
            if cmd.target_c < nominal or sent < nominal:
                return None, False, (REJECT_TEMP_LOW, f"{cmd.target_c} < nominal {nominal}")
            return nozzle_temp_rpc(sent), sent > self.temp_target_c, None
        if isinstance(cmd, SetSpeedFactor):
            if cmd.percent != 100 and cmd.percent not in cfg.ladder.speed_steps_percent:
                return None, False, (REJECT_SPEED, f"{cmd.percent}% is not a configured step")
            return speed_factor_rpc(cmd.percent), cmd.percent < self.speed_percent, None
        if not cfg.ladder.purge_enabled:
            return None, False, (REJECT_PURGE, "purge is disabled in the config")
        if not is_safe_macro(cfg.ladder.purge_macro):
            return None, False, (REJECT_PURGE, "purge_macro looks like a raw G-code word")
        return purge_rpc(cfg.ladder.purge_macro), True, None

    async def _send(
        self, cmd: Command, rpc: RpcCall, now_s: float, result: Callable[..., CommandResult]
    ) -> CommandResult:
        start = self._clock()
        note: str | None = None
        try:
            await asyncio.wait_for(
                self._link.call(rpc.method, dict(rpc.params)), self._cfg.command_ack_timeout_s
            )
        except TimeoutError:
            note = f"{NOT_ACKED}: no ack within {self._cfg.command_ack_timeout_s}s"
        except Exception as exc:  # transport or JSON-RPC error
            note = f"{NOT_ACKED}: {type(exc).__name__}: {exc}"[:300]
        acked = None if note else now_s + (self._clock() - start)
        return result(
            cmd, accepted=True, rejected_reason=note, rpc_method=rpc.method, sent_at_s=now_s,
            acked_at_s=acked,
        )

    async def _deliver_notify(
        self, cmd: Notify, now_s: float, result: Callable[..., CommandResult]
    ) -> CommandResult:
        start = self._clock()
        note: str | None = None
        try:
            await asyncio.wait_for(
                self._notifier.notify(cmd.level, cmd.message), self._cfg.command_ack_timeout_s
            )
        except TimeoutError:
            note = f"{NOT_ACKED}: notifier timed out"
        except Exception as exc:
            note = f"{NOT_ACKED}: notifier {type(exc).__name__}: {exc}"[:300]
        acked = None if note else now_s + (self._clock() - start)
        return result(
            cmd, accepted=True, rejected_reason=note, rpc_method=NOTIFIER_TARGET, sent_at_s=now_s,
            acked_at_s=acked,
        )
