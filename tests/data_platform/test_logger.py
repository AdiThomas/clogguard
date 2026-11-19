import argparse
import asyncio
import json
from datetime import UTC, datetime

import numpy as np
import pytest
import yaml

from clogguard.core.channels import ChannelConfig
from clogguard.core.config import MaterialProfile
from clogguard.core.schema import RunMetadata
from clogguard.logger import RunLogger, http_snapshot, make_run_id
from clogguard.logger import cli as log_cli
from clogguard.moonraker import MoonrakerClient
from clogguard.store import RunStore, load_raw, read_meta, validate_run, write_meta
from tests.data_platform.fake_moonraker import FakeMoonraker

PLA = MaterialProfile(max_temp_c=220, nominal_temp_c=210)
START = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

CHANNELS = {
    "version": 1,
    "channels": [
        {
            "name": "extruder_power",
            "unit": "ratio",
            "rate_hz": 4,
            "source": "moonraker",
            "params": {"object": "extruder", "field": "power"},
        },
        {
            "name": "e_velocity_mm_s",
            "unit": "mm/s",
            "rate_hz": 10,
            "source": "moonraker",
            "role": "context",
            "params": {"object": "motion_report", "field": "live_extruder_velocity"},
        },
        {
            "name": "z_mm",
            "unit": "mm",
            "rate_hz": 1,
            "source": "moonraker",
            "role": "context",
            "params": {"object": "toolhead", "field": "position", "index": 2},
        },
        {
            "name": "volumetric_flow_mm3_s",
            "unit": "mm3/s",
            "rate_hz": 10,
            "source": "derived",
            "role": "context",
            "params": {"fn": "volumetric_flow", "inputs": ["e_velocity_mm_s"]},
        },
        {
            "name": "accel_rms",
            "unit": "m/s2",
            "rate_hz": 10,
            "source": "serial",
            "params": {"port": "/dev/fake", "raw_rate_hz": 200},
        },
        {"name": "true_severity", "unit": "1", "rate_hz": 10, "source": "sim", "role": "label"},
    ],
}


def initial_status():
    return {
        "print_stats": {
            "state": "standby",
            "filename": "",
            "print_duration": 0.0,
            "info": {"current_layer": None},
        },
        "extruder": {"temperature": 210.0, "target": 210.0, "power": 0.30},
        "motion_report": {"live_extruder_velocity": 0.0},
        "toolhead": {"position": [0.0, 0.0, 0.2, 0.0]},
    }


async def fake_serial(port, baud):
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(0.02)
        yield json.dumps({"t_mcu": loop.time() - 500.0, "accel_rms": [0.5, 0.7, 0.9, 1.1]})


@pytest.fixture
async def server():
    s = FakeMoonraker(initial_status())
    await s.start()
    yield s
    await s.stop()


@pytest.fixture
async def client(server):
    c = MoonrakerClient(server.url, api_key_env=None)
    await c.connect()
    yield c
    await c.close()


def test_make_run_id():
    assert make_run_id(START, "gcodes/My Cube (v2).gcode") == "20261119T120000_My-Cube-v2"
    assert make_run_id(START, "") == "20261119T120000_print"
    assert len(make_run_id(START, "x" * 200)) == 80


async def test_end_to_end_standby_printing_complete(server, client, tmp_path):
    shots = []

    async def snap():
        shots.append(1)
        if len(shots) == 2:
            raise OSError("camera hiccup")  # must not stop logging
        return b"\xff\xd8fakejpeg"

    run_id = make_run_id(START, "cube.gcode")
    run_dir = tmp_path / run_id
    # the service already started this run: its audit log and meta fields must survive
    run_dir.mkdir()
    (run_dir / "audit.jsonl").write_text('{"seq": 0}\n')
    write_meta(
        run_dir,
        RunMetadata(
            run_id=run_id,
            data_source="real",
            started_at=START,
            service_active=True,
            service_mode="watch",
            alert_time_s=0.6,
        ),
    )

    logger = RunLogger(
        client,
        ChannelConfig.model_validate(CHANNELS),
        runs_dir=tmp_path,
        material=PLA,
        purpose="baseline",
        operator="tester",
        snapshot=snap,
        photo_on_layer=True,
        photo_interval_s=0.5,
        flush_interval_s=0.3,
        line_reader_factory=fake_serial,
        wall_clock=lambda: START,
    )
    assert "true_severity" not in logger.sampler.columns  # sim channel skipped
    task = asyncio.create_task(logger.run(max_runs=1))
    await asyncio.sleep(0.1)
    await server.notify(
        {
            "print_stats": {
                "state": "printing",
                "filename": "cube.gcode",
                "info": {"current_layer": 1},
            }
        }
    )
    for i in range(14):
        diff = {
            "extruder": {"power": 0.4 + 0.01 * i},
            "motion_report": {"live_extruder_velocity": 2.0},
        }
        if i == 7:
            diff["print_stats"] = {"info": {"current_layer": 2}}
            diff["toolhead"] = {"position": [1.0, 1.0, 0.4, 0.0]}
        await server.notify(diff)
        await asyncio.sleep(0.1)
    await server.notify({"print_stats": {"state": "paused"}})
    await asyncio.sleep(0.1)
    await server.notify({"print_stats": {"state": "printing"}})  # resume: same run
    await asyncio.sleep(0.1)
    await server.notify({"print_stats": {"state": "complete"}})
    runs = await asyncio.wait_for(task, 5)

    assert runs == [run_dir]
    assert (run_dir / "audit.jsonl").read_text() == '{"seq": 0}\n'
    assert not (run_dir / ".parts").exists()
    run = RunStore(tmp_path).load(run_id)
    meta = run.meta
    assert meta.outcome == "completed"
    assert meta.data_source == "real" and meta.purpose == "baseline"
    assert meta.operator == "tester" and meta.gcode_file == "cube.gcode"
    assert meta.service_active and meta.service_mode == "watch" and meta.alert_time_s == 0.6
    assert meta.channels == [
        "extruder_power",
        "e_velocity_mm_s",
        "z_mm",
        "accel_rms",
        "volumetric_flow_mm3_s",
    ]
    assert meta.software_version and len(meta.config_hash) == 12
    assert validate_run(run, run_dir=run_dir) == []

    f = run.frame
    assert 15 <= len(f) <= 22
    assert np.allclose(np.diff(f["t"]), 0.1)
    assert f["t"].iloc[0] == 0.0
    assert f["extruder_power"].iloc[-1] == pytest.approx(0.53)
    assert set(f["z_mm"].dropna()) <= {0.2, 0.4} and f["z_mm"].iloc[-1] == 0.4
    late = f[f["t"] > 0.3]
    assert np.allclose(late["volumetric_flow_mm3_s"], 2.0 * PLA.filament_area_mm2)
    assert np.allclose(late["accel_rms"].dropna(), 0.8)  # mean of each burst
    raw = load_raw(run_dir, "accel_rms")
    assert raw is not None and len(raw) > 40 and set(raw["value"]) == {0.5, 0.7, 0.9, 1.1}

    # photos: at start, on layer change, periodically; the failed shot is skipped
    assert len(meta.photos) >= 2
    assert all((run_dir / p.path).read_bytes() == b"\xff\xd8fakejpeg" for p in meta.photos)
    assert [p.t_s for p in meta.photos] == sorted(p.t_s for p in meta.photos)


async def test_mid_print_join_and_stop(server, client, tmp_path):
    server.status["print_stats"].update(state="printing", filename="a.gcode", print_duration=300.0)
    logger = RunLogger(
        client,
        ChannelConfig.model_validate(CHANNELS),
        runs_dir=tmp_path,
        material=PLA,
        line_reader_factory=fake_serial,
        flush_interval_s=60,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(logger.run(stop=stop))
    await asyncio.sleep(0.35)
    stop.set()
    (run_dir,) = await asyncio.wait_for(task, 5)
    meta = read_meta(run_dir)
    assert meta.outcome is None
    assert "joined mid-print" in meta.notes and "stopped before" in meta.notes
    assert (run_dir / "timeseries.parquet").exists()


async def test_standby_means_cancelled_and_error_means_failed(server, client, tmp_path):
    channels = ChannelConfig.model_validate({"channels": CHANNELS["channels"][:2]})
    logger = RunLogger(client, channels, runs_dir=tmp_path, keep_raw=False)
    task = asyncio.create_task(logger.run(max_runs=2))
    await asyncio.sleep(0.05)
    for end in ("standby", "error"):
        await server.notify({"print_stats": {"state": "printing", "filename": f"{end}.gcode"}})
        await asyncio.sleep(0.25)
        await server.notify({"print_stats": {"state": end}})
        await asyncio.sleep(1.05)  # distinct run ids (1 s resolution)
    runs = await asyncio.wait_for(task, 5)
    outcomes = [read_meta(r).outcome for r in runs]
    assert outcomes == ["cancelled", "failed"]


async def test_serial_failure_is_fatal(server, client, tmp_path):
    async def broken(port, baud):
        raise RuntimeError("needs pyserial")
        yield b""  # pragma: no cover

    logger = RunLogger(
        client,
        ChannelConfig.model_validate(CHANNELS),
        runs_dir=tmp_path,
        material=PLA,
        line_reader_factory=broken,
    )
    with pytest.raises(RuntimeError, match="pyserial"):
        await asyncio.wait_for(logger.run(), 5)


async def test_http_snapshot_bad_url():
    snap = http_snapshot("http://127.0.0.1:1/snapshot", timeout_s=0.5)
    with pytest.raises(Exception):  # noqa: B017 - any transport error
        await snap()


async def test_cli_run(server, tmp_path, monkeypatch):
    svc = {
        "moonraker_url": server.url,
        "material": {"max_temp_c": 220, "nominal_temp_c": 210},
        "runs_dir": str(tmp_path / "runs"),
    }
    (tmp_path / "svc.yaml").write_text(yaml.safe_dump(svc))
    chans = {"channels": CHANNELS["channels"][:2]}
    (tmp_path / "ch.yaml").write_text(yaml.safe_dump(chans))
    args = log_cli.build_parser().parse_args(
        [
            "--service-config",
            str(tmp_path / "svc.yaml"),
            "--channels",
            str(tmp_path / "ch.yaml"),
            "--purpose",
            "validation_clean",
            "--max-runs",
            "1",
            "--operator",
            "op",
        ]
    )
    assert isinstance(args, argparse.Namespace)
    task = asyncio.create_task(log_cli._run(args))
    await server.wait_conns(1)
    await asyncio.sleep(0.1)
    await server.notify({"print_stats": {"state": "printing", "filename": "c.gcode"}})
    await asyncio.sleep(0.3)
    await server.notify({"print_stats": {"state": "complete"}})
    assert await asyncio.wait_for(task, 5) == 0
    (meta,) = RunStore(tmp_path / "runs").list()
    assert meta.purpose == "validation_clean" and meta.outcome == "completed"
    assert meta.operator == "op"
