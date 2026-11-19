"""Unit tests of the service's smaller parts: bus, row builder, links, audit helpers, CLI."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import pytest
import yaml

from clogguard.core.channels import ChannelConfig
from clogguard.core.commands import Notify, SetSpeedFactor
from clogguard.core.events import CommandResult, Lifecycle
from clogguard.core.schema import RunMetadata
from clogguard.service import cli
from clogguard.service.audit import (
    AuditWriter,
    find_logger_run,
    make_run_id,
    read_audit,
    update_meta,
)
from clogguard.service.bus import EventBus
from clogguard.service.link import InMemoryLink, MoonrakerLink
from clogguard.service.rows import StatusRowBuilder, extract, subscription_objects
from tests.service.test_runtime import CHANNELS

WALL = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


# -- bus -------------------------------------------------------------------------------------
async def test_bus_drops_oldest_for_slow_subscriber() -> None:
    bus: EventBus[int] = EventBus(default_maxsize=3)
    slow = bus.subscribe()
    fast = bus.subscribe(maxsize=100)
    for i in range(10):
        bus.publish(i)
    assert [slow.queue.get_nowait() for _ in range(3)] == [7, 8, 9] and slow.dropped == 7
    assert await fast.get() == 0
    it = fast.__aiter__()
    assert await it.__anext__() == 1
    fast.close()
    fast.close()  # idempotent
    assert bus.subscriber_count == 1


# -- rows ------------------------------------------------------------------------------------
def test_extract() -> None:
    assert extract({"position": [1, 2, 3.5]}, {"field": "position", "index": 2}) == 3.5
    assert extract({"drv": {"sg": 7}}, {"field": "drv", "key": "sg"}) == 7.0
    assert extract({"ok": True}, {"field": "ok"}) == 1.0
    assert math.isnan(extract({"drv": None}, {"field": "drv", "key": "sg"}))
    assert math.isnan(extract({}, {"field": "x"}))
    assert math.isnan(extract({"x": "text"}, {"field": "x"}))


def test_subscription_objects_merges_fields() -> None:
    cc = ChannelConfig.model_validate({"channels": [
        {"name": "a", "unit": "u", "rate_hz": 1, "source": "moonraker",
         "params": {"object": "extruder", "field": "power"}},
        {"name": "b", "unit": "u", "rate_hz": 1, "source": "moonraker",
         "params": {"object": "extruder", "field": "power"}},
        {"name": "c", "unit": "u", "rate_hz": 1, "source": "moonraker",
         "params": {"object": "fan"}},
        {"name": "d", "unit": "u", "rate_hz": 1, "source": "moonraker",
         "params": {"object": "fan", "field": "speed"}},
        {"name": "e", "unit": "u", "rate_hz": 1, "source": "moonraker", "params": {}},
    ]})
    objs = subscription_objects(cc)
    assert objs["extruder"] == ["power"] and objs["fan"] is None
    assert objs["webhooks"] is None


def test_row_builder_zero_order_hold_and_gaps() -> None:
    rb = StatusRowBuilder(CHANNELS, start_eventtime=100.0, filament_area_mm2=2.0,
                          stale_after_s=1.0)
    assert rb.freshness(0.0) == (False, "no printer status yet")
    assert rb.take_rows(0.5).empty
    rb.update({"extruder": {"power": 0.5, "temperature": 210},
               "motion_report": {"live_extruder_velocity": 3.0},
               "toolhead": {"position": [0, 0, 1.2]}}, 100.5)
    rb.update({"extruder": {"power": 0.7}}, 100.75)
    df = rb.take_rows(101.0 - 100.0)
    assert list(df["t"]) == pytest.approx([0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    assert list(df["extruder_power"]) == [0.5, 0.5, 0.5, 0.7, 0.7, 0.7]
    assert df["volumetric_flow_mm3_s"].iloc[0] == 6.0 and df["z_mm"].iloc[0] == 1.2
    assert rb.freshness(1.0) == (True, "")
    assert rb.age("extruder_power", 1.0) == pytest.approx(0.25)
    assert rb.age("z_mm", 1.0) == pytest.approx(0.5) and rb.last_update_t == 0.75
    # long gap: rows stop stale_after_s after the last update, then resume
    rb.update({"extruder": {"power": 0.1}}, 110.0)
    df2 = rb.take_rows(10.2)
    assert df2["t"].max() == pytest.approx(10.2) and df2["t"].min() == pytest.approx(1.1)
    assert not ((df2["t"] > 1.8) & (df2["t"] < 9.9)).any()
    assert not rb.freshness(20.0)[0]
    rb.update({"extruder": {"power": None}}, 110.5)
    assert "missing" in rb.freshness(10.5)[1]


def test_row_builder_ignores_unknown_derived() -> None:
    cc = ChannelConfig.model_validate({"channels": [
        {"name": "v", "unit": "u", "rate_hz": 1, "source": "derived",
         "params": {"fn": "volumetric_flow"}},
    ]})
    rb = StatusRowBuilder(cc, start_eventtime=0, filament_area_mm2=1, stale_after_s=1)
    rb.update({}, 0.0)
    assert math.isnan(rb.values["v"]) and rb.age("v", 0.0) == 0.0


# -- links -----------------------------------------------------------------------------------
async def test_in_memory_link_failure_modes() -> None:
    link = InMemoryLink(connected=False)
    with pytest.raises(ConnectionError):
        await link.call("x", {})
    with pytest.raises(ConnectionError):
        await link.subscribe({})
    link.reconnect()
    link.ack_delay_s = 0.001
    assert await link.call("m", {"a": 1}) == "ok" and link.calls == [("m", {"a": 1})]
    with pytest.raises(TimeoutError):
        link.push({"x": {}}, 1.0)
        await link.settle(timeout=0.01)


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.last_eventtime: float | None = 12.5
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[Any] = []
        self.closed = False

    async def connect(self, retry: bool = False) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        return "ok"

    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
        return {"extruder": {"power": 0.1}}

    async def updates(self) -> AsyncIterator[tuple[dict[str, Any], float]]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


async def test_moonraker_link_adapter() -> None:
    client = FakeClient()
    link = MoonrakerLink(client, poll_s=0.01)
    await link.start()
    assert link.connected
    assert await link.call("m", {"p": 1}) == "ok" and client.calls == [("m", {"p": 1})]
    assert await link.subscribe({"extruder": None}) == {
        "eventtime": 12.5, "status": {"extruder": {"power": 0.1}}}
    client.last_eventtime = None
    assert math.isnan((await link.subscribe({}))["eventtime"])
    client.queue.put_nowait(({"a": {}}, 1.0))
    got = []

    async def consume() -> None:
        async for item in link.updates():
            got.append(item)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.03)
    client.connected = False  # stream ends on lost connection
    await asyncio.wait_for(task, 1)
    assert got == [({"a": {}}, 1.0)]
    client.connected = True
    client.queue.put_nowait(None)  # client closed: stream ends
    await asyncio.wait_for(consume(), 1)
    await link.close()
    assert client.closed


def test_moonraker_link_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from tests.safety.helpers import make_cfg

    real = importlib.import_module

    def fake_import(name: str, *a: Any) -> Any:
        if name == "clogguard.moonraker":
            raise ImportError("absent")
        return real(name, *a)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError, match="MoonrakerClient is not available"):
        MoonrakerLink.from_config(make_cfg())

    class Mod:
        class MoonrakerClient:
            def __init__(self, url: str, api_key_env: str) -> None:
                self.url, self.env = url, api_key_env

    monkeypatch.setattr(importlib, "import_module", lambda name, *a: Mod)
    link = MoonrakerLink.from_config(make_cfg())
    assert link._client.url.startswith("ws://") and link._client.env == "MOONRAKER_API_KEY"


# -- audit helpers ---------------------------------------------------------------------------
def test_make_run_id() -> None:
    assert make_run_id(WALL, "gcodes/My Part (v2).gcode") == "20261119T120000_My-Part-v2"
    assert make_run_id(WALL, "") == "20261119T120000_print"
    assert len(make_run_id(WALL, "x" * 200 + ".gcode")) == 80


def test_find_logger_run_matching_rule(tmp_path: Path) -> None:
    assert find_logger_run(tmp_path / "missing", "a.gcode", WALL) is None

    def write(name: str, gcode: str, dt: float) -> None:
        d = tmp_path / name
        d.mkdir()
        m = RunMetadata(run_id=name, data_source="real", gcode_file=gcode,
                        started_at=(WALL + timedelta(seconds=dt)).replace(tzinfo=None))
        (d / "meta.json").write_text(m.model_dump_json())

    write("far", "a.gcode", 60)
    write("other_file", "b.gcode", 0)
    write("close", "dir/a.gcode", 3)
    write("closer", "a.gcode", -1)
    write("no_file", "", 0)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "meta.json").write_text("{not json")
    assert find_logger_run(tmp_path, "a.gcode", WALL) == ("closer", tmp_path / "closer")
    assert find_logger_run(tmp_path, "c.gcode", WALL) is None
    assert find_logger_run(tmp_path, "", WALL) is None


def test_audit_writer_continues_seq_and_update_meta_edge_cases(tmp_path: Path) -> None:
    w = AuditWriter(tmp_path / "r", "r")
    w.write(Lifecycle(t_s=0, wall_time=WALL, what="print_start"))
    w.close()
    w.close()
    w.write(Lifecycle(t_s=0, wall_time=WALL, what="print_end"))  # after close: not written
    w2 = AuditWriter(tmp_path / "r", "r")
    assert w2.write(Lifecycle(t_s=1, wall_time=WALL, what="print_end")).seq == 1
    w2.close()
    assert [r.seq for r in read_audit(tmp_path / "r" / "audit.jsonl")] == [0, 1]
    res = [CommandResult(t_s=1, wall_time=WALL, command=Notify(level="info", message="m"),
                         accepted=True)]
    assert not update_meta(tmp_path / "r", mode="act", alert_time_s=None, results=res)
    (tmp_path / "r" / "meta.json").write_text("{}")
    assert not update_meta(tmp_path / "r", mode="act", alert_time_s=None, results=res)
    m = RunMetadata(run_id="r", data_source="real", started_at=WALL)
    (tmp_path / "r" / "meta.json").write_text(m.model_dump_json())
    res.append(CommandResult(t_s=2, wall_time=WALL, command=SetSpeedFactor(percent=85),
                             accepted=False, rejected_reason="min_interval: x"))
    assert update_meta(tmp_path / "r", mode="act", alert_time_s=3.0, results=res)
    out = RunMetadata.model_validate_json((tmp_path / "r" / "meta.json").read_text())
    assert [(a.kind, a.accepted, a.detail) for a in out.actions] == [
        ("set_speed_factor", False, {"percent": 85, "acked": False})]


# -- CLI -------------------------------------------------------------------------------------
class DummyModel:
    def make_online(self) -> Any:
        return None


def _write_configs(tmp_path: Path) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    svc = yaml.safe_load((root / "config/service.example.yaml").read_text())
    svc["detector"]["model_path"] = str(tmp_path / "missing.joblib")
    sp = tmp_path / "service.yaml"
    sp.write_text(yaml.safe_dump(svc))
    return sp, root / "config/channels.example.yaml"


def test_cli_missing_files_and_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sp, cp = _write_configs(tmp_path)
    assert cli.main(["--config", str(tmp_path / "nope.yaml")]) == 2
    assert cli.main(["--config", str(sp), "--channels", str(cp)]) == 2
    assert "model file not found" in capsys.readouterr().err
    bad = tmp_path / "bad.joblib"
    joblib.dump({"not": "a model"}, bad)
    assert cli.main(["--config", str(sp), "--channels", str(cp), "--model", str(bad)]) == 2
    assert "no make_online" in capsys.readouterr().err
    svc = yaml.safe_load(sp.read_text())
    svc["channels_file"] = str(tmp_path / "none.yaml")
    sp.write_text(yaml.safe_dump(svc))
    assert cli.main(["--config", str(sp)]) == 2


def test_cli_runs_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sp, cp = _write_configs(tmp_path)
    model = tmp_path / "m.joblib"
    joblib.dump(DummyModel(), model)
    seen: dict[str, Any] = {}

    async def fake_serve(config: Any, channels: Any, m: Any) -> None:
        seen["mode"], seen["model"] = config.mode, m

    monkeypatch.setattr(cli, "_serve", fake_serve)
    assert cli.main(["--config", str(sp), "--channels", str(cp), "--model", str(model)]) == 0
    assert seen["mode"] == "watch" and isinstance(seen["model"], DummyModel)

    async def failing(*a: Any) -> None:
        raise RuntimeError("moonraker client missing")

    monkeypatch.setattr(cli, "_serve", failing)
    assert cli.main(["--config", str(sp), "--channels", str(cp), "--model", str(model)]) == 2


async def test_cli_serve_wires_link_and_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from clogguard.service import link as link_mod
    from clogguard.service import runtime as runtime_mod
    from tests.safety.helpers import make_cfg

    events: list[str] = []

    class L:
        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            events.append("close")

    class S:
        def __init__(self, *a: Any) -> None:
            pass

        def stop(self) -> None:
            pass

        async def run(self) -> None:
            events.append("run")

    monkeypatch.setattr(link_mod.MoonrakerLink, "from_config", classmethod(lambda c, cfg: L()))
    monkeypatch.setattr(runtime_mod, "Service", S)
    await cli._serve(make_cfg(), CHANNELS, DummyModel())
    assert events == ["start", "run", "close"]
