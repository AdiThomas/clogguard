"""The recovery ladder as a pure state machine, with scripted assessments."""

from __future__ import annotations

from typing import Any

from clogguard.core.commands import Notify, Pause, RunPurge, SetNozzleTemp, SetSpeedFactor
from clogguard.core.config import DetectorConfig, ServiceConfig
from clogguard.core.events import Lifecycle, Recovery
from clogguard.service.ladder import LadderState, RecoveryLadder, build_levels, describe
from tests.safety.helpers import make_cfg
from tests.service.helpers import WALL, A, commands, drive, result


def cfg(mode: str = "act", **ladder: Any) -> ServiceConfig:
    lad = {"speed_steps_percent": [85, 70], "temp_step_c": 5.0, "max_temp_raise_c": 10.0,
           **ladder}
    c = make_cfg(mode, nominal=210, max_temp=230, **lad)
    c.detector = DetectorConfig(warmup_s=10.0)
    return c


def test_levels() -> None:
    kinds = [(lv.kind, lv.value) for lv in build_levels(cfg(purge_enabled=True))]
    assert kinds == [("normal", None), ("warn", None), ("speed", 85), ("speed", 70),
                     ("temp", 215), ("temp", 220), ("purge", None), ("pause", None)]
    # ceiling 217 (max_temp) stops at 215
    c = make_cfg(nominal=210, max_temp=217, max_temp_raise_c=15)
    assert [lv.value for lv in build_levels(c) if lv.kind == "temp"] == [215]


def test_warmup_suppresses_everything() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: 0.99, 9.0, extra=lambda t: {"recoverable": False})
    assert all(d.command is None and d.reason == "warmup" for d in out)
    assert lad.state is LadderState.WARMUP


def test_full_climb_to_pause() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: 0.95, 80)
    cmds = commands(out)
    assert [c for _, c in cmds] == [
        cmds[0][1], SetSpeedFactor(percent=85), SetSpeedFactor(percent=70),
        SetNozzleTemp(target_c=215), SetNozzleTemp(target_c=220),
        Pause(reason="top of ladder reached without recovery"),
    ]
    assert isinstance(cmds[0][1], Notify) and cmds[0][0] == 10 and lad.first_warn_t_s == 10
    times = [t for t, c in cmds if not isinstance(c, Notify)]
    assert times == [11, 21, 31, 41, 51]
    assert lad.state is LadderState.PAUSED and lad.interventions == 4
    assert out[-1].command is None and "paused" in out[-1].reason


def test_purge_level_used_when_enabled() -> None:
    lad = RecoveryLadder(cfg(purge_enabled=True))
    cmds = [c for _, c in commands(drive(lad, lambda t: 0.95, 70))]
    assert RunPurge() in cmds and isinstance(cmds[-1], Pause)
    assert cmds.index(RunPurge()) == cmds.index(SetNozzleTemp(target_c=220)) + 1


def test_verified_recovery_steps_down_to_normal() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: 0.95 if t < 36 else 0.1, 90)
    cmds = commands(out)
    printer = [(t, c) for t, c in cmds if not isinstance(c, Notify)]
    assert printer == [
        (11, SetSpeedFactor(percent=85)), (21, SetSpeedFactor(percent=70)),
        (31, SetNozzleTemp(target_c=215)),
        (46, SetNozzleTemp(target_c=210)),  # temperature restored first
        # the below-watch streak restarts after each action: 47 + 10 = 57, 58 + 10 = 68
        (57, SetSpeedFactor(percent=85)), (68, SetSpeedFactor(percent=100)),
    ]
    events = lad.drain_events()
    assert [type(e) for e in events] == [Recovery] * 4
    assert all(isinstance(e, Recovery) and e.verified for e in events)
    assert lad.state is LadderState.NORMAL and lad.level == 0
    assert any(d.reason == "recovered; back to normal" and d.t_s == 79 for d in out)
    assert lad.speed == 100 and lad.temp == 210


def test_reclimb_when_p_rises_during_step_down() -> None:
    lad = RecoveryLadder(cfg())

    def p(t: float) -> float:
        if t < 26:
            return 0.95
        return 0.5 if t >= 40 else 0.1

    printer = [(t, c) for t, c in commands(drive(lad, p, 60)) if not isinstance(c, Notify)]
    # 11: 85, 21: 70, verified at 36 -> 85, window ends 46 with p >= watch -> 70 again
    assert printer == [(11, SetSpeedFactor(percent=85)), (21, SetSpeedFactor(percent=70)),
                       (36, SetSpeedFactor(percent=85)), (46, SetSpeedFactor(percent=70)),
                       (56, SetNozzleTemp(target_c=215))]


def test_warning_clears_without_action() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: 0.65 if t < 11 else 0.1, 30)
    assert [type(c) for _, c in commands(out)] == [Notify]
    assert lad.state is LadderState.NORMAL
    assert any(d.reason == "warning cleared" for d in out)


def test_unrecoverable_pauses_directly() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: 0.9 if t >= 15 else 0.1, 20,
                extra=lambda t: {"recoverable": t < 15})
    cmds = commands(out)
    assert cmds == [(15, Pause(reason="clog assessed as unrecoverable"))]
    lad2 = RecoveryLadder(cfg(pause_if_unrecoverable=False))
    cmds2 = commands(drive(lad2, lambda t: 0.9, 20, extra=lambda t: {"recoverable": False}))
    assert not any(isinstance(c, Pause) for _, c in cmds2)


def test_intervention_cap_pauses_and_notifies() -> None:
    lad = RecoveryLadder(cfg(max_interventions_per_print=2))
    out = drive(lad, lambda t: 0.95, 40)
    cmds = commands(out)
    assert cmds[-2] == (31, Pause(reason="intervention cap reached"))
    assert isinstance(cmds[-1][1], Notify) and cmds[-1][1].level == "critical"
    ev = lad.drain_events()
    assert any(isinstance(e, Lifecycle) and e.what == "intervention_cap_reached" for e in ev)


def test_heat_creep_reverts_and_skips_heat() -> None:
    lad = RecoveryLadder(cfg(purge_enabled=True))
    out = drive(lad, lambda t: 0.95, 60, extra=lambda t: {"severity": 0.3 if t <= 31 else 0.5})
    printer = [(t, c) for t, c in commands(out) if not isinstance(c, Notify)]
    assert printer[2] == (31, SetNozzleTemp(target_c=215))
    assert printer[3] == (41, SetNozzleTemp(target_c=210))
    assert printer[4] == (51, RunPurge())  # 220 skipped
    assert lad.heat_disabled
    assert any("heat-creep" in d.reason for d in out)


def test_unknown_or_stale_holds() -> None:
    lad = RecoveryLadder(cfg())
    out = drive(lad, lambda t: None if t % 2 else 0.95, 30,
                extra=lambda t: {"data_fresh": not (t % 2)})
    assert all(d.command is None for d in out if d.t_s % 2)
    lad2 = RecoveryLadder(cfg())
    out2 = drive(lad2, lambda t: 0.95, 30, extra=lambda t: {"data_fresh": False})
    assert commands(out2) == [] and all("stale" in d.reason for d in out2 if d.t_s >= 10)


def test_watch_mode_only_notifies_on_paper() -> None:
    lad = RecoveryLadder(cfg("watch"))
    out = drive(lad, lambda t: 0.95, 80)
    cmds = commands(out)
    assert all(isinstance(c, Notify) for _, c in cmds)
    reasons = [d.reason for d in out if d.command is not None]
    assert "would set speed factor 85%" in reasons[1]
    assert "would set nozzle temperature 215.0 C" in reasons[3]
    assert "would pause the print" in reasons[-1] and cmds[-1][1].level == "critical"  # type: ignore[union-attr]
    assert lad.first_warn_t_s == 10 and lad.state is LadderState.PAUSED


def test_unacked_command_only_escalates_to_pause() -> None:
    lad = RecoveryLadder(cfg())
    lad.step(A(10, 0.95))  # warn
    d = lad.step(A(11, 0.95))
    assert d.command == SetSpeedFactor(percent=85)
    assert lad.step(A(11.5, 0.95)).reason.startswith("awaiting")
    lad.on_result(result(d.command, 11, acked=False))
    assert lad.state is LadderState.DEGRADED
    q = lad.step(A(12, 0.1))
    assert isinstance(q.command, Notify) and q.command.level == "critical"
    assert lad.step(A(13, 0.1)).command is None
    assert lad.step(A(14, 0.7)).command is None  # below act, window not over
    p = lad.step(A(21, 0.5))  # window over, still >= watch
    assert isinstance(p.command, Pause)
    assert lad.speed == 100  # never treated as applied


def test_unacked_degraded_pauses_on_act_threshold() -> None:
    lad = RecoveryLadder(cfg())
    lad.step(A(10, 0.95))
    d = lad.step(A(11, 0.95))
    lad.on_result(result(d.command, 11, acked=False))  # type: ignore[arg-type]
    lad.take_queued(11, WALL)
    assert isinstance(lad.step(A(12, 0.9)).command, Pause)


def test_rejected_command_reverts_and_retries_later() -> None:
    lad = RecoveryLadder(cfg())
    lad.step(A(10, 0.95))
    d = lad.step(A(11, 0.95))
    lad.on_result(result(d.command, 11, accepted=False))  # type: ignore[arg-type]
    assert lad.state is LadderState.WARNED
    assert lad.step(A(15, 0.95)).command is None
    assert lad.step(A(21, 0.95)).command == SetSpeedFactor(percent=85)


def test_unacked_pause_is_retried() -> None:
    lad = RecoveryLadder(cfg())
    d = lad.step(A(15, 0.9, recoverable=False))
    lad.on_result(result(d.command, 15, acked=False))  # type: ignore[arg-type]
    assert lad.state is LadderState.PAUSE_PENDING
    q = lad.take_queued(15, WALL)
    assert q is not None and isinstance(q.command, Notify)
    again = lad.step(A(16, None))  # retried even with no data
    assert isinstance(again.command, Pause)
    lad.on_result(result(again.command, 16))
    assert lad.state is LadderState.PAUSED


def test_notify_results_are_ignored_and_describe() -> None:
    lad = RecoveryLadder(cfg())
    lad.on_result(result(Notify(level="info", message="x"), 1))
    assert lad.state is LadderState.WARMUP
    assert describe(RunPurge()) == "run purge macro"
    assert describe(Notify(level="info", message="x")) == "notify (info)"


def test_rewarn_after_full_step_down_and_rejected_step_down() -> None:
    lad = RecoveryLadder(cfg(speed_steps_percent=[85]))
    out = drive(lad, lambda t: 0.95 if t < 15 else (0.1 if t < 30 else 0.5), 50)
    cmds = commands(out)
    # 10 warn, 11: 85, verified at 25 -> 100 (level 0), window ends 35 with p rising since 30
    assert cmds[2] == (25, SetSpeedFactor(percent=100))
    assert isinstance(cmds[3][1], Notify) and cmds[3][0] == 35
    assert lad.first_warn_t_s == 10 and lad.state is LadderState.WARNED

    lad2 = RecoveryLadder(cfg(speed_steps_percent=[85]))
    drive(lad2, lambda t: 0.95 if t < 15 else 0.1, 24)
    d = lad2.step(A(25, 0.1))
    assert d.command == SetSpeedFactor(percent=100)
    lad2.on_result(result(d.command, 25, accepted=False))
    assert lad2.step(A(26, 0.1)).reason == "recovered; waiting to step down"
    assert lad2.step(A(35, 0.1)).command == SetSpeedFactor(percent=100)
