"""Unit tests of the safety gate: every rule, every branch."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import BaseModel

from clogguard.core.commands import Notify, Pause, RunPurge, SetNozzleTemp, SetSpeedFactor
from clogguard.core.events import AuditRecord
from clogguard.safety import NOT_ACKED, SafetyGate, was_applied
from clogguard.safety import gate as gate_mod
from clogguard.safety.templates import (
    GCODE_METHOD,
    PAUSE_METHOD,
    format_temp,
    matches_template,
    nozzle_temp_rpc,
    purge_rpc,
)
from clogguard.service.link import InMemoryLink
from tests.safety.helpers import FakeClock, RecordingNotifier, make_cfg


def gate_for(cfg: Any = None, **link_kw: Any) -> tuple[SafetyGate, InMemoryLink, RecordingNotifier]:
    link = InMemoryLink(**link_kw)
    notifier = RecordingNotifier()
    return SafetyGate(cfg or make_cfg(), link, notifier, clock=FakeClock()), link, notifier


async def submit(g: SafetyGate, cmd: object, now: float = 100.0, fresh: Any = True):
    return await g.submit(cmd, now_s=now, decision_t_s=now, data_fresh=fresh)


def code(r: Any) -> str:
    return (r.rejected_reason or "").split(":")[0]


# -- watch mode ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cmd",
    [SetSpeedFactor(percent=85), SetNozzleTemp(target_c=215), RunPurge(), Pause(reason="x")],
)
async def test_watch_mode_rejects_every_printer_kind(cmd: Any) -> None:
    g, link, _ = gate_for(make_cfg("watch", purge_enabled=True))
    r = await submit(g, cmd)
    assert not r.accepted and code(r) == gate_mod.REJECT_WATCH
    assert link.calls == []


async def test_watch_mode_delivers_notify() -> None:
    g, link, notifier = gate_for(make_cfg("watch"))
    r = await submit(g, Notify(level="warning", message="hi"))
    assert r.accepted and r.acked_at_s == 100.0 and r.rpc_method == gate_mod.NOTIFIER_TARGET
    assert notifier.sent == [("warning", "hi")] and link.calls == []


# -- accepted commands use the exact templates ---------------------------------------------
async def test_speed_temp_purge_pause_templates() -> None:
    g, link, _ = gate_for(make_cfg(purge_enabled=True, purge_macro="CLOGGUARD_MY_PURGE"))
    r1 = await submit(g, SetSpeedFactor(percent=85), now=100)
    r2 = await submit(g, SetNozzleTemp(target_c=215.0), now=110)
    r3 = await submit(g, RunPurge(), now=120)
    r4 = await submit(g, Pause(reason="p"), now=121)
    assert all(was_applied(r) for r in (r1, r2, r3, r4))
    assert link.calls == [
        (GCODE_METHOD, {"script": "M220 S85"}),
        (GCODE_METHOD, {"script": "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=215.0"}),
        (GCODE_METHOD, {"script": "CLOGGUARD_MY_PURGE"}),
        (PAUSE_METHOD, {}),
    ]
    assert r1.sent_at_s == 100 and r1.acked_at_s == 100 and r1.decision_t_s == 100
    assert g.interventions == 3


async def test_dict_command_is_parsed() -> None:
    g, link, _ = gate_for()
    r = await submit(g, {"kind": "set_speed_factor", "percent": 70})
    assert was_applied(r) and isinstance(r.command, SetSpeedFactor)
    assert link.calls[0][1] == {"script": "M220 S70"}


# -- value limits: reject, never adjust -------------------------------------------------------
async def test_over_ceiling_temperature_rejected_not_lowered() -> None:
    g, link, _ = gate_for()  # ceiling = min(230, 210 + 10) = 220
    r = await submit(g, SetNozzleTemp(target_c=225))
    assert not r.accepted and code(r) == gate_mod.REJECT_TEMP_HIGH
    assert r.command == SetNozzleTemp(target_c=225)  # recorded as asked
    assert link.calls == []


async def test_rounding_cannot_push_over_ceiling() -> None:
    g, link, _ = gate_for(make_cfg(nominal=210, max_temp=219.97))
    r = await submit(g, SetNozzleTemp(target_c=219.96))  # would be sent as 220.0
    assert code(r) == gate_mod.REJECT_TEMP_HIGH and link.calls == []


async def test_below_nominal_rejected() -> None:
    g, link, _ = gate_for()
    r = await submit(g, SetNozzleTemp(target_c=200))
    assert code(r) == gate_mod.REJECT_TEMP_LOW and link.calls == []
    g2, link2, _ = gate_for(make_cfg(nominal=210.04))
    r2 = await submit(g2, SetNozzleTemp(target_c=210.04))  # would be sent as 210.0
    assert code(r2) == gate_mod.REJECT_TEMP_LOW and link2.calls == []


async def test_off_step_speed_rejected_and_100_allowed() -> None:
    g, link, _ = gate_for()
    r = await submit(g, SetSpeedFactor(percent=80))
    assert code(r) == gate_mod.REJECT_SPEED
    r2 = await submit(g, SetSpeedFactor(percent=100))
    assert was_applied(r2) and link.calls == [(GCODE_METHOD, {"script": "M220 S100"})]
    assert g.interventions == 0  # a restore is not corrective


async def test_purge_disabled_rejected() -> None:
    g, link, _ = gate_for(make_cfg(purge_enabled=False))
    r = await submit(g, RunPurge())
    assert code(r) == gate_mod.REJECT_PURGE and link.calls == []


@pytest.mark.parametrize("macro", ["M112", "G28", "M84"])
async def test_raw_gcode_purge_macro_rejected(macro: str) -> None:
    g, link, _ = gate_for(make_cfg(purge_enabled=True, purge_macro=macro))
    r = await submit(g, RunPurge())
    assert code(r) == gate_mod.REJECT_PURGE and link.calls == []
    assert matches_template(GCODE_METHOD, {"script": macro}, macro) is None


# -- freshness, interval, cap ------------------------------------------------------------------
@pytest.mark.parametrize("fresh", [False, None, 1, "yes"])
async def test_stale_data_blocks_corrections_but_not_pause(fresh: Any) -> None:
    g, link, _ = gate_for()
    r = await submit(g, SetSpeedFactor(percent=85), fresh=fresh)
    assert code(r) == gate_mod.REJECT_STALE
    p = await submit(g, Pause(reason="stale"), fresh=fresh)
    assert was_applied(p) and link.calls == [(PAUSE_METHOD, {})]


async def test_non_finite_time_is_treated_as_stale() -> None:
    g, link, _ = gate_for()
    r = await submit(g, SetSpeedFactor(percent=85), now=math.nan)
    assert code(r) == gate_mod.REJECT_STALE and link.calls == []


async def test_min_interval_enforced_pause_exempt() -> None:
    g, link, _ = gate_for()
    assert was_applied(await submit(g, SetSpeedFactor(percent=85), now=100))
    r = await submit(g, SetSpeedFactor(percent=70), now=105)
    assert code(r) == gate_mod.REJECT_INTERVAL
    back = await submit(g, SetSpeedFactor(percent=70), now=95)  # clock went backwards
    assert code(back) == gate_mod.REJECT_INTERVAL
    assert was_applied(await submit(g, Pause(reason="p"), now=106))
    assert was_applied(await submit(g, SetSpeedFactor(percent=70), now=110))
    assert len(link.calls) == 3


async def test_intervention_cap_counts_escalations_only() -> None:
    g, link, _ = gate_for(make_cfg(max_interventions_per_print=2))
    assert was_applied(await submit(g, SetSpeedFactor(percent=85), now=0))
    assert was_applied(await submit(g, SetNozzleTemp(target_c=215), now=10))
    r = await submit(g, SetSpeedFactor(percent=70), now=20)
    assert code(r) == gate_mod.REJECT_CAP
    # restores still allowed after the cap
    assert was_applied(await submit(g, SetNozzleTemp(target_c=210), now=30))
    assert was_applied(await submit(g, SetSpeedFactor(percent=100), now=40))
    assert was_applied(await submit(g, Pause(reason="cap"), now=41))
    assert g.interventions == 2 and len(link.calls) == 5


async def test_reset_for_new_print() -> None:
    g, _, _ = gate_for(make_cfg(max_interventions_per_print=1))
    await submit(g, SetSpeedFactor(percent=85), now=0)
    assert g.interventions == 1 and g.last_action_s == 0
    g.reset_for_new_print()
    assert g.interventions == 0 and g.last_action_s is None and g.speed_percent == 100
    assert g.temp_target_c == 210
    assert was_applied(await submit(g, SetSpeedFactor(percent=85), now=1))


# -- delivery failures -----------------------------------------------------------------------
async def test_missing_ack_is_sent_but_not_applied() -> None:
    g, link, _ = gate_for(make_cfg(ack_timeout=0.02), hang=True)
    r = await submit(g, SetSpeedFactor(percent=85), now=100)
    assert r.accepted and r.acked_at_s is None and r.sent_at_s == 100
    assert r.error is not None and r.error.startswith(NOT_ACKED) and r.rejected_reason is None
    assert not was_applied(r) and len(link.calls) == 1
    # counted as sent for the interval rule (conservative)
    assert code(await submit(g, SetSpeedFactor(percent=70), now=101)) == gate_mod.REJECT_INTERVAL


async def test_transport_error_is_noted() -> None:
    g, _, _ = gate_for(fail_with=ConnectionError("boom"))
    r = await submit(g, Pause(reason="p"))
    assert r.accepted and r.acked_at_s is None and "ConnectionError: boom" in (
        r.error or "")


async def test_ack_time_uses_gate_clock() -> None:
    clock = FakeClock(0.0)

    class SlowLink(InMemoryLink):
        async def call(self, method: str, params: dict[str, Any]) -> Any:
            clock.advance(0.7)
            return await super().call(method, params)

    g = SafetyGate(make_cfg(), SlowLink(), RecordingNotifier(), clock=clock)
    r = await g.submit(SetSpeedFactor(percent=85), now_s=50, decision_t_s=49.5, data_fresh=True)
    assert r.acked_at_s == pytest.approx(50.7) and r.acked_at_s - r.decision_t_s <= 2.0


async def test_notifier_failure_and_timeout() -> None:
    cfg = make_cfg(ack_timeout=0.02)
    g = SafetyGate(cfg, InMemoryLink(), RecordingNotifier(fail_with=RuntimeError("x")))
    r = await submit(g, Notify(level="info", message="m"))
    assert r.accepted and r.acked_at_s is None and "RuntimeError" in (r.error or "")
    g2 = SafetyGate(cfg, InMemoryLink(), RecordingNotifier(hang=True))
    r2 = await submit(g2, Notify(level="info", message="m"))
    assert r2.acked_at_s is None and "timed out" in (r2.error or "")


# -- untrusted objects -----------------------------------------------------------------------
class _BadDump(BaseModel):
    def model_dump(self, **kw: Any) -> dict[str, Any]:  # type: ignore[override]
        raise RuntimeError("cannot dump")


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        (SetSpeedFactor.model_construct(percent=999), gate_mod.REJECT_INVALID),
        (SetSpeedFactor.model_construct(percent=5), gate_mod.REJECT_INVALID),
        (SetNozzleTemp.model_construct(target_c=float("nan")), gate_mod.REJECT_NON_FINITE),
        (SetNozzleTemp.model_construct(target_c=float("inf")), gate_mod.REJECT_NON_FINITE),
        (SetSpeedFactor.model_construct(kind="raw_gcode", percent=85), gate_mod.REJECT_KIND),
        ({"kind": "gcode", "script": "M112"}, gate_mod.REJECT_KIND),
        ("M112", gate_mod.REJECT_INVALID),
        (None, gate_mod.REJECT_INVALID),
        (_BadDump(), gate_mod.REJECT_INVALID),
    ],
)
async def test_invalid_objects_rejected(obj: object, expected: str) -> None:
    g, link, notifier = gate_for(make_cfg(purge_enabled=True))
    r = await submit(g, obj)
    assert not r.accepted and code(r) == expected
    assert link.calls == [] and notifier.sent == []
    assert isinstance(r.command, Notify) and "placeholder" in r.command.message
    rec = AuditRecord(seq=0, run_id="x", event=r)
    assert AuditRecord.model_validate_json(rec.model_dump_json()) == rec


async def test_smuggled_extra_field_is_ignored() -> None:
    g, link, _ = gate_for(make_cfg(purge_enabled=True))
    r = await submit(g, RunPurge.model_construct(macro="M112"))
    assert was_applied(r) and link.calls == [(GCODE_METHOD, {"script": "CLOGGUARD_PURGE"})]


# -- templates ---------------------------------------------------------------------------------
def test_matches_template() -> None:
    m = "CLOGGUARD_PURGE"
    assert matches_template(PAUSE_METHOD, {}, m) == "pause"
    assert matches_template(PAUSE_METHOD, {"x": 1}, m) is None
    assert matches_template("printer.gcode.other", {"script": "M220 S85"}, m) is None
    assert matches_template(GCODE_METHOD, {"script": "M220 S85", "x": 1}, m) is None
    assert matches_template(GCODE_METHOD, {"script": 5}, m) is None
    assert matches_template(GCODE_METHOD, {"script": "M220 S85"}, m) == "speed"
    assert matches_template(GCODE_METHOD, nozzle_temp_rpc(215).params, m) == "temp"
    assert matches_template(GCODE_METHOD, {"script": m}, m) == "purge"
    assert matches_template(GCODE_METHOD, {"script": "M112"}, m) is None
    assert matches_template(GCODE_METHOD, {"script": "M220 S85\nM112"}, m) is None


def test_template_builders() -> None:
    assert format_temp(214.96) == "215.0"
    with pytest.raises(ValueError):
        purge_rpc("M112\nG28")
    with pytest.raises(ValueError):
        purge_rpc("M112")
    assert purge_rpc("CLOGGUARD_PURGE").params == {"script": "CLOGGUARD_PURGE"}
