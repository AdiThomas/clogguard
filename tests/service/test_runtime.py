"""The asyncio service end to end against InMemoryLink and scripted scorers (simulated data)."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from clogguard.core.channels import ChannelConfig
from clogguard.core.config import DetectorConfig, ServiceConfig
from clogguard.core.events import Assessment, AuditRecord, CommandResult, Decision, Lifecycle
from clogguard.core.schema import RunMetadata
from clogguard.safety.templates import GCODE_METHOD
from clogguard.service.audit import read_audit
from clogguard.service.bus import EventBus
from clogguard.service.link import InMemoryLink
from clogguard.service.runtime import Service
from tests.safety.helpers import FakeClock, RecordingNotifier, make_cfg

WALL0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
START_ET = 1000.0

CHANNELS = ChannelConfig.model_validate({"channels": [
    {"name": "extruder_power", "unit": "ratio", "rate_hz": 4, "source": "moonraker",
     "role": "signal", "params": {"object": "extruder", "field": "power"}},
    {"name": "extruder_temp_c", "unit": "degC", "rate_hz": 4, "source": "moonraker",
     "role": "context", "params": {"object": "extruder", "field": "temperature"}},
    {"name": "e_velocity_mm_s", "unit": "mm/s", "rate_hz": 10, "source": "moonraker",
     "role": "context", "params": {"object": "motion_report", "field": "live_extruder_velocity"}},
    {"name": "z_mm", "unit": "mm", "rate_hz": 1, "source": "moonraker", "role": "context",
     "params": {"object": "toolhead", "field": "position", "index": 2}},
    {"name": "volumetric_flow_mm3_s", "unit": "mm3/s", "rate_hz": 10, "source": "derived",
     "role": "context", "params": {"fn": "volumetric_flow", "inputs": ["e_velocity_mm_s"]}},
    {"name": "force_g", "unit": "g", "rate_hz": 10, "source": "serial", "role": "signal",
     "params": {"port": "/dev/null"}},
]})

IDLE_STATUS = {
    "print_stats": {"state": "standby", "filename": "", "print_duration": 0.0},
    "pause_resume": {"is_paused": False},
    "webhooks": {"state": "ready"},
    "extruder": {"temperature": 210.0, "target": 210.0, "power": 0.4},
    "motion_report": {"live_extruder_velocity": 0.0},
    "toolhead": {"position": [0.0, 0.0, 0.2, 0.0]},
}


class FnScorer:
    def __init__(self, p_of_t: Callable[[float], float | None], **extra: Any) -> None:
        self.p_of_t = p_of_t
        self.extra = extra
        self.rows: list[pd.DataFrame] = []

    def update(self, rows: pd.DataFrame, now_s: float) -> Assessment:
        self.rows.append(rows)
        p = self.p_of_t(now_s)
        state = "unknown" if p is None else ("clog" if p >= 0.8 else "normal")
        return Assessment(t_s=now_s, wall_time=datetime.now(UTC), state=state, p_clog=p,
                          model_id="fn", minutes_to_defect=5.0, **self.extra)


def svc_cfg(tmp_path: Path, mode: str = "act", **ladder: Any) -> ServiceConfig:
    lad = {"speed_steps_percent": [85], "temp_step_c": 5.0, "max_temp_raise_c": 5.0,
           "verify_window_s": 5.0, "min_action_interval_s": 5.0, **ladder}
    c = make_cfg(mode, nominal=210, max_temp=230, **lad)
    c.detector = DetectorConfig(warmup_s=5.0, stale_after_s=3.0)
    c.runs_dir = str(tmp_path / "runs")
    return c


class AckLink(InMemoryLink):
    """Each call advances the fake clock by ``latency`` (simulated ack latency)."""

    def __init__(self, clock: FakeClock, latency: float = 0.8, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.latency = latency

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.clock.advance(self.latency)
        return await super().call(method, params)


class Harness:
    def __init__(self, tmp_path: Path, cfg: ServiceConfig,
                 scorer_factory: Callable[[], Any], *, link: InMemoryLink | None = None,
                 clock: Callable[[], float] = time.monotonic, budget: float = 0.5) -> None:
        self.link = link or InMemoryLink(status=IDLE_STATUS, eventtime=START_ET - 5)
        self.notifier = RecordingNotifier()
        self.bus: EventBus[AuditRecord] = EventBus()
        self.sub = self.bus.subscribe(maxsize=100_000)
        self.wall = WALL0
        self.svc = Service(self.link, CHANNELS, cfg, scorer_factory, notifier=self.notifier,
                           bus=self.bus, clock=clock, wall_clock=lambda: self.wall,
                           scorer_budget_s=budget, poll_s=0.01)
        self.task: asyncio.Task[None] | None = None
        self.et = START_ET - 5

    async def __aenter__(self) -> Harness:
        self.task = asyncio.create_task(self.svc.run())
        await self.wait_subscribed(1)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.svc.stop()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)

    async def wait_subscribed(self, n: int) -> None:
        for _ in range(500):
            if len(self.link.subscriptions) >= n and self.link._waiting:
                return
            await asyncio.sleep(0.005)
        raise TimeoutError("not subscribed")

    def start_print(self, filename: str = "part.gcode", et: float = START_ET) -> None:
        self.et = et
        self.link.push({"print_stats": {"state": "printing", "filename": filename}}, et)

    def run_for(self, seconds: float, hz: float = 4.0, power: float = 0.5,
                extra: Callable[[float], dict[str, Any]] | None = None) -> None:
        n = round(seconds * hz)
        for _ in range(n):
            self.et = round(self.et + 1.0 / hz, 6)
            diff: dict[str, Any] = {"motion_report": {"live_extruder_velocity": 2.0},
                                    "extruder": {"power": power}}
            if extra:
                diff.update(extra(self.et - START_ET))
            self.link.push(diff, self.et)

    def end_print(self, state: str = "complete") -> None:
        self.et += 0.25
        self.link.push({"print_stats": {"state": state}}, self.et)

    def events(self) -> list[AuditRecord]:
        out = []
        while not self.sub.queue.empty():
            out.append(self.sub.queue.get_nowait())
        return out


def of_type(recs: list[AuditRecord], cls: type) -> list[Any]:
    return [r.event for r in recs if isinstance(r.event, cls)]


def lifecycle(recs: list[AuditRecord], what: str) -> list[Lifecycle]:
    return [e for e in of_type(recs, Lifecycle) if e.what == what]


def clog_then_clear(t: float) -> float:
    return 0.95 if 10 <= t < 18 else 0.1


# ---------------------------------------------------------------------------------------------
async def test_act_mode_full_cycle_and_audit(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    link = AckLink(clock, status=IDLE_STATUS, eventtime=START_ET - 5)
    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, lambda: FnScorer(clog_then_clear), link=link,
                       clock=clock) as h:
        h.start_print()
        h.run_for(40)
        h.end_print()
        await link.settle()
        run_id = h.svc.completed_runs[0]
    # exact printer calls: climb, verified recovery, step-down to normal
    assert [p["script"] for m, p in link.calls] == [
        "M220 S85",
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=215.0",
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=210.0",
        "M220 S100",
    ]
    assert all(m == GCODE_METHOD for m, _ in link.calls)
    recs = read_audit(Path(cfg.runs_dir) / run_id / "audit.jsonl")
    assert [r.seq for r in recs] == list(range(len(recs)))
    assert all(r.run_id == run_id for r in recs)
    # every record roundtrips
    for r in recs:
        assert AuditRecord.model_validate_json(r.model_dump_json()) == r
    assessments = of_type(recs, Assessment)
    ts = [a.t_s for a in assessments]
    assert ts == sorted(ts) and max(b - a for a, b in itertools.pairwise(ts)) <= 1.0 + 1e-6
    assert len(assessments) >= 40  # >= 1 per second of printer time
    results = [r for r in of_type(recs, CommandResult) if r.command.kind != "notify"]
    assert len(results) == 4
    for r in results:
        assert r.accepted and r.acked_at_s is not None
        assert r.acked_at_s - r.decision_t_s <= 2.0
        assert r.acked_at_s - r.sent_at_s == pytest.approx(0.8)
    recov = [e for e in (r.event for r in recs) if e.type == "recovery"]
    assert len(recov) >= 2 and all(e.verified for e in recov)
    whats = [e.what for e in of_type(recs, Lifecycle)]
    assert whats[0] == "print_start" and whats[-1] == "print_end"
    assert h.notifier.sent and h.notifier.sent[0][0] == "warning"
    # nothing printer-related was decided during warmup
    assert all(d.command is None for d in of_type(recs, Decision) if d.t_s < 5)


async def test_watch_mode_never_calls_printer_and_updates_meta(tmp_path: Path) -> None:
    cfg = svc_cfg(tmp_path, mode="watch")
    # a logger created the run dir first
    logger_dir = Path(cfg.runs_dir) / "20261119T120001_part"
    logger_dir.mkdir(parents=True)
    meta = RunMetadata(run_id="20261119T120001_part", data_source="simulated",
                       started_at=WALL0 + timedelta(seconds=1), gcode_file="gcodes/part.gcode")
    (logger_dir / "meta.json").write_text(meta.model_dump_json())
    async with Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.95 if t >= 10 else 0.1)) as h:
        h.start_print()
        h.run_for(60)
        h.end_print()
        await h.link.settle()
    assert h.link.calls == []
    assert h.svc.completed_runs == ["20261119T120001_part"]
    recs = read_audit(logger_dir / "audit.jsonl")
    decisions = [d for d in of_type(recs, Decision) if d.command is not None]
    assert decisions and all(d.command.kind == "notify" for d in decisions)
    assert any("would set speed factor 85%" in d.reason for d in decisions)
    assert any("would pause" in d.reason for d in decisions)
    updated = RunMetadata.model_validate_json((logger_dir / "meta.json").read_text())
    assert updated.service_active and updated.service_mode == "watch"
    assert updated.alert_time_s == pytest.approx(10.0) and updated.actions == []


async def test_meta_actions_recorded_in_act_mode(tmp_path: Path) -> None:
    cfg = svc_cfg(tmp_path)
    d = Path(cfg.runs_dir) / "logger_run"
    d.mkdir(parents=True)
    meta = RunMetadata(run_id="logger_run", data_source="real", started_at=WALL0,
                       gcode_file="part.gcode")
    (d / "meta.json").write_text(meta.model_dump_json())
    async with Harness(tmp_path, cfg, lambda: FnScorer(clog_then_clear)) as h:
        h.start_print()
        h.run_for(40)
        h.end_print()
        await h.link.settle()
    updated = RunMetadata.model_validate_json((d / "meta.json").read_text())
    assert [a.kind for a in updated.actions] == ["set_speed_factor", "set_nozzle_temp",
                                                 "set_nozzle_temp", "set_speed_factor"]
    assert updated.actions[0].detail == {"percent": 85, "acked": True}
    assert updated.service_mode == "act" and updated.alert_time_s == pytest.approx(10.0)


async def test_print_start_end_detection(tmp_path: Path) -> None:
    made: list[FnScorer] = []

    def factory() -> FnScorer:
        made.append(FnScorer(lambda t: 0.95 if t >= 6 else 0.1))
        return made[-1]

    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, factory) as h:
        h.link.push({"extruder": {"power": 0.3}}, START_ET - 1)  # idle: no print
        await h.link.settle()
        assert h.svc.current_run_id is None and made == []
        h.start_print("a.gcode")
        h.run_for(8)
        await h.link.settle()
        assert h.svc.gate.interventions == 1  # 6 warn, 7 slow down
        h.end_print("cancelled")
        await h.link.settle()
        assert h.svc.current_run_id is None
        h.wall = WALL0 + timedelta(minutes=5)
        h.start_print("b.gcode", et=h.et + 10)
        h.run_for(2)
        await h.link.settle()
        assert h.svc.gate.interventions == 0  # gate reset for the new print
        # filename change while printing = a new print
        h.wall = WALL0 + timedelta(minutes=9)
        h.link.push({"print_stats": {"state": "printing", "filename": "c.gcode"}}, h.et + 1)
        await h.link.settle()
    assert len(made) == 3
    assert h.svc.completed_runs == ["20261119T120000_a", "20261119T120500_b"]
    recs = read_audit(Path(cfg.runs_dir) / "20261119T120000_a" / "audit.jsonl")
    assert lifecycle(recs, "print_end")[0].detail == "state=cancelled"
    assert of_type(recs, Assessment)[0].t_s == 0.0  # time base = eventtime - start
    assert (Path(cfg.runs_dir) / "20261119T120900_c" / "audit.jsonl").exists()
    assert sum(len(r) for r in made[0].rows) >= 70  # ~10 Hz base-rate rows for 8 s


async def test_disconnect_holds_and_resumes(tmp_path: Path) -> None:
    cfg = svc_cfg(tmp_path)
    scorer = FnScorer(lambda t: 0.95 if t >= 6 else 0.1)
    async with Harness(tmp_path, cfg, lambda: scorer) as h:
        h.start_print()
        h.run_for(5)
        await h.link.settle()
        h.events()
        h.link.disconnect()
        await asyncio.sleep(0.05)
        recs = h.events()
        assert lifecycle(recs, "printer_disconnected") and lifecycle(recs, "data_stale")
        assert h.link.calls == []
        run_id = h.svc.current_run_id
        h.link.reconnect()
        await h.wait_subscribed(2)
        h.run_for(10)
        await h.link.settle()
        recs = h.events()
        assert lifecycle(recs, "printer_ready") and lifecycle(recs, "data_restored")
        assert h.svc.current_run_id == run_id  # same print continues
        assert len(h.link.calls) == 2  # corrections resume once data is fresh (t=7, t=12)


async def test_scorer_error_and_slow_scorer_hold(tmp_path: Path) -> None:
    class Boom:
        def update(self, rows: pd.DataFrame, now_s: float) -> Assessment:
            raise RuntimeError("model broke")

    class Slow(FnScorer):
        def update(self, rows: pd.DataFrame, now_s: float) -> Assessment:
            time.sleep(0.15)
            return super().update(rows, now_s)

    class Wrong:
        def update(self, rows: pd.DataFrame, now_s: float) -> Any:
            return {"p_clog": 0.99}

    for factory, why in [(Boom, "scorer error"), (lambda: Slow(lambda t: 0.99), "budget"),
                         (Wrong, "non-Assessment")]:
        cfg = svc_cfg(tmp_path)
        async with Harness(tmp_path, cfg, factory, budget=0.05) as h:
            h.start_print()
            h.run_for(8, hz=2)
            await h.link.settle()
            recs = h.events()
        assert h.link.calls == []
        assert all(a.state == "unknown" and not a.data_fresh for a in of_type(recs, Assessment))
        assert any(why in e.detail for e in lifecycle(recs, "data_stale")), why


async def test_missing_signal_channel_is_stale(tmp_path: Path) -> None:
    status = json.loads(json.dumps(IDLE_STATUS))
    status["extruder"]["power"] = None  # signal channel not available
    link = InMemoryLink(status=status, eventtime=START_ET - 5)
    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.99), link=link) as h:
        h.start_print()
        h.run_for(10, power=None)  # type: ignore[arg-type]
        await link.settle()
        recs = h.events()
    assert link.calls == []
    assert any("missing signal channels" in e.detail for e in lifecycle(recs, "data_stale"))
    assert all(d.command is None for d in of_type(recs, Decision))


async def test_paused_printer_holds(tmp_path: Path) -> None:
    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.99)) as h:
        h.start_print()
        h.link.push({"print_stats": {"state": "paused"}, "pause_resume": {"is_paused": True}},
                    START_ET + 0.1)
        h.run_for(10)
        await h.link.settle()
        recs = h.events()
        assert h.svc.current_run_id is not None  # paused keeps the print
    assert h.link.calls == [] and h.notifier.sent == []
    assert all("paused" in d.reason for d in of_type(recs, Decision) if d.t_s > 0)


async def test_join_running_print_uses_print_duration(tmp_path: Path) -> None:
    status = json.loads(json.dumps(IDLE_STATUS))
    status["print_stats"] = {"state": "printing", "filename": "x.gcode", "print_duration": 100.0}
    link = InMemoryLink(status=status, eventtime=5000.0)
    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.1), link=link) as h:
        await link.settle()
        ctx = h.svc.current_print
        assert ctx is not None and ctx.start_et == pytest.approx(4900.0)
        recs = h.events()
    assert of_type(recs, Assessment)[0].t_s == pytest.approx(100.0)


async def test_watchdog_flags_silence(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    cfg = svc_cfg(tmp_path)
    async with Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.1), clock=clock) as h:
        h.start_print()
        h.run_for(3)
        await h.link.settle()
        h.events()
        clock.advance(10)
        await asyncio.sleep(0.9)
        recs = h.events()
        assert any("no printer updates" in e.detail for e in lifecycle(recs, "data_stale"))
        h.run_for(1)
        await h.link.settle()
        assert lifecycle(h.events(), "data_restored")


async def test_stop_and_cancel_are_clean(tmp_path: Path) -> None:
    cfg = svc_cfg(tmp_path)
    h = Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.1))
    async with h:
        h.start_print()
        h.run_for(2)
        await h.link.settle()
        run_id = h.svc.current_run_id
    recs = read_audit(Path(cfg.runs_dir) / str(run_id) / "audit.jsonl")
    assert recs[-1].event.type == "lifecycle" and recs[-1].event.what == "service_stop"
    assert h.link.calls == []
    # cancelling run() also shuts down cleanly
    h2 = Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.1))
    task = asyncio.create_task(h2.svc.run())
    await h2.wait_subscribed(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lifecycle(h2.events(), "service_stop")


async def test_waits_for_connection_and_retries_subscribe(tmp_path: Path) -> None:
    class Flaky(InMemoryLink):
        fails = 2

        async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
            if self.fails:
                self.fails -= 1
                raise ConnectionError("not yet")
            return await super().subscribe(objects)

    link = Flaky(status=IDLE_STATUS, connected=False)
    cfg = svc_cfg(tmp_path)
    h = Harness(tmp_path, cfg, lambda: FnScorer(lambda t: 0.1), link=link)
    task = asyncio.create_task(h.svc.run())
    await asyncio.sleep(0.03)
    assert link.subscriptions == []
    link.reconnect()
    await h.wait_subscribed(1)
    objs = link.subscriptions[0]
    assert objs["extruder"] == ["power", "temperature"] and objs["print_stats"] is None
    assert "force_g" not in json.dumps(objs)
    h.svc.stop()
    await task
