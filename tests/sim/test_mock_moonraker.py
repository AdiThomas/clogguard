from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import pytest
from websockets.asyncio.client import connect

from clogguard.sim.mock_moonraker import MockMoonraker
from clogguard.sim.scenarios import preset

ALL = {"extruder": None, "print_stats": None, "gcode_move": None, "motion_report": None,
       "pause_resume": None, "clogguard_sim": None, "tmc2209 extruder": None,
       "load_cell": None, "webhooks": None, "fan": None, "toolhead": None}


class Client:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.ids = itertools.count(1)
        self.notes: list[dict] = []
        self.pending: dict[int, asyncio.Future] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            else:
                self.notes.append(msg)

    async def call(self, method: str, params: dict | None = None) -> dict:
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method,
                                       "params": params or {}, "id": rid}))
        return await asyncio.wait_for(fut, 10)

    def updates(self) -> list[tuple[dict, float]]:
        return [tuple(n["params"]) for n in self.notes if n["method"] == "notify_status_update"]


async def _client(mock: MockMoonraker) -> Client:
    ws = await connect(mock.url)
    return Client(ws)


async def test_identify_subscribe_and_diffs():
    async with MockMoonraker(preset("normal_box"), speedup=200, start_delay_s=1.0) as mock:
        assert mock.url.endswith("/websocket")
        c = await _client(mock)
        r = await c.call("server.connection.identify",
                         {"client_name": "t", "version": "0", "type": "agent", "url": "x"})
        assert isinstance(r["result"]["connection_id"], int)
        lst = await c.call("printer.objects.list")
        assert "clogguard_sim" in lst["result"]["objects"]
        sub = await c.call("printer.objects.subscribe", {"objects": {
            **ALL, "extruder": ["temperature", "power"], "nonexistent": None}})
        status = sub["result"]["status"]
        assert set(status["extruder"]) == {"temperature", "power"}
        assert "nonexistent" not in status
        assert status["print_stats"]["state"] == "standby"
        await mock.wait_for(lambda m: m.sim.print_t > 30)
        await asyncio.sleep(0.05)
        ups = c.updates()
        assert len(ups) > 100
        times = [et for _, et in ups]
        assert all(b > a for a, b in itertools.pairwise(times))
        assert all(set(d["extruder"]) <= {"temperature", "power"} for d, _ in ups
                   if "extruder" in d)
        assert any("force_g" in d.get("clogguard_sim", {}) for d, _ in ups)
        assert any(d.get("print_stats", {}).get("state") == "printing" for d, _ in ups)
        # diffs only: webhooks never changes, so it is never re-sent
        assert not any("webhooks" in d for d, _ in ups)
        assert c.notes[0]["method"] == "notify_klippy_ready"
        q = await c.call("printer.objects.query", {"objects": {"gcode_move": ["speed_factor"]}})
        assert q["result"]["status"] == {"gcode_move": {"speed_factor": 1.0}}
        assert q["result"]["eventtime"] > 1000.0
        await c.ws.close()


async def test_gcode_commands_change_the_simulation():
    async with MockMoonraker(preset("normal_box"), speedup=1000, start_delay_s=0.5) as mock:
        c = await _client(mock)
        await c.call("printer.objects.subscribe", {"objects": {"gcode_move": ["speed_factor"]}})
        await mock.wait_for(lambda m: m.sim.print_t > 700)
        r = await c.call("printer.gcode.script", {"script": "M220 S50"})
        assert r["result"] == "ok"
        t_cmd = mock.sim.print_t
        assert mock.sim.speed_factor == pytest.approx(0.5)
        await mock.wait_for(lambda m: m.sim.print_t > t_cmd + 120)
        f = mock.run_frame()
        before = f[(f.t > t_cmd - 100) & (f.t <= t_cmd)].volumetric_flow_mm3_s.mean()
        after = f[(f.t > t_cmd + 5) & (f.t <= t_cmd + 105)].volumetric_flow_mm3_s.mean()
        assert after < 0.75 * before
        await asyncio.sleep(0.05)
        assert any(d.get("gcode_move", {}).get("speed_factor") == 0.5 for d, _ in c.updates())

        r = await c.call("printer.gcode.script",
                         {"script": "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=215"})
        assert r["result"] == "ok" and mock.sim.target_c == 215.0
        r = await c.call("printer.gcode.script",
                         {"script": "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=400"})
        assert "error" in r and mock.sim.target_c == 215.0

        r = await c.call("printer.gcode.script", {"script": "CLOGGUARD_PURGE"})
        assert r["result"] == "ok"
        assert mock.sim.purge_count == 1 and mock.sim.purge_remaining_s <= 0

        log = [e for e in mock.command_log if e["method"] == "printer.gcode.script"]
        assert {"sim_time_s", "wall_time", "params", "ok"} <= set(log[0])
        assert log[0]["params"]["script"] == "M220 S50"
        await c.ws.close()


@pytest.mark.parametrize("script", ["G28", "M220 S50; G1 X0", "M104 S300", "FIRMWARE_RESTART",
                                    "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60", ""])
async def test_unknown_gcode_is_an_error(script):
    async with MockMoonraker(preset("normal_box"), speedup=100) as mock:
        c = await _client(mock)
        r = await c.call("printer.gcode.script", {"script": script})
        assert r["error"]["code"] == 400
        assert mock.sim.speed_factor == 1.0 and mock.sim.target_c == 210.0
        r = await c.call("printer.firmware_restart")
        assert r["error"]["code"] == -32601
        await c.ws.close()


async def test_pause_resume_cancel_change_state():
    async with MockMoonraker(preset("normal_box"), speedup=400, start_delay_s=200.0) as mock:
        c = await _client(mock)
        r = await c.call("printer.print.pause")
        assert "error" in r  # standby: no active print
        await mock.wait_for_state("printing")
        await mock.wait_for(lambda m: m.sim.print_t > 20)
        assert (await c.call("printer.print.pause"))["result"] == "ok"
        tau = mock.sim.tau
        await mock.wait_for(lambda m: m.sim.print_t > 40)
        assert mock.sim.tau == tau  # job frozen
        q = await c.call("printer.objects.query",
                         {"objects": {"print_stats": ["state"], "pause_resume": None,
                                      "motion_report": ["live_extruder_velocity"]}})
        st = q["result"]["status"]
        assert st["print_stats"]["state"] == "paused"
        assert st["pause_resume"]["is_paused"] is True
        assert st["motion_report"]["live_extruder_velocity"] == 0.0
        assert (await c.call("printer.print.resume"))["result"] == "ok"
        await mock.wait_for(lambda m: m.sim.tau > tau)
        assert (await c.call("printer.print.cancel"))["result"] == "ok"
        assert mock.state == "cancelled"
        await c.ws.close()


async def test_full_print_completes():
    async with MockMoonraker(preset("slow_viscous"), speedup=8000, start_delay_s=2000.0) as mock:
        c = await _client(mock)
        await c.call("printer.objects.subscribe", {"objects": {"print_stats": ["state"]}})
        await mock.wait_for_state("complete", timeout=40)
        await asyncio.sleep(0.05)
        states = [d["print_stats"]["state"] for d, _ in c.updates() if "print_stats" in d]
        assert states == ["printing", "complete"]
        f = mock.run_frame()
        assert f.t.iloc[-1] > 30 * 60
        assert f.true_defect.max() == 1.0  # ground truth available
        await c.ws.close()


async def test_bad_requests():
    async with MockMoonraker(preset("normal_box"), speedup=100) as mock:
        c = await _client(mock)
        await c.ws.send("not json")
        await c.ws.send(json.dumps({"jsonrpc": "2.0", "params": {}, "id": 99}))
        await asyncio.sleep(0.1)
        errs = [n for n in c.notes if "error" in n]
        assert {e["error"]["code"] for e in errs} == {-32700, -32600}
        r = await c.call("printer.objects.query", {"objects": ["extruder"]})
        assert r["error"]["code"] == -32602
        await c.ws.close()
