"""A mock Moonraker server backed by the clog simulator.

Impersonates Moonraker's websocket JSON-RPC API as described in the shared wire assumption
(``/websocket``; identify, objects list/query/subscribe, ``notify_status_update`` diffs,
``printer.gcode.script``, pause/resume/cancel) while running a simulated print in accelerated
time. Everything it serves is simulated.

Example::

    async with MockMoonraker(preset("slow_viscous"), speedup=60) as mock:
        ...connect a client to mock.url...
        frame = mock.run_frame()  # ground truth so far
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import re
import time
from collections import deque
from typing import Any

import pandas as pd
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from clogguard.sim.physics import PhysicsParams
from clogguard.sim.scenarios import PRESETS, Scenario, preset
from clogguard.sim.simulator import DT_S, Simulator

OBJECTS = (
    "webhooks",
    "print_stats",
    "pause_resume",
    "extruder",
    "motion_report",
    "gcode_move",
    "toolhead",
    "fan",
    "tmc2209 extruder",
    "load_cell",
    "clogguard_sim",
)

ERR_PARSE = -32700
ERR_INVALID = -32600
ERR_METHOD = -32601
ERR_PARAMS = -32602
ERR_KLIPPY = 400  # Moonraker relays Klipper G-code errors as HTTP-style 400

_RE_M220 = re.compile(r"^M220 S(\d{1,3})$")
_RE_TEMP = re.compile(r"^SET_HEATER_TEMPERATURE HEATER=extruder TARGET=(\d{1,3}(?:\.\d+)?)$")


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Client:
    _ids = itertools.count(1)

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.connection_id = next(self._ids)
        self.subscription: dict[str, list[str] | None] = {}
        self.last_sent: dict[str, dict[str, Any]] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.identified: dict[str, Any] = {}


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


class MockMoonraker:
    """Async context manager running a websocket JSON-RPC mock of Moonraker.

    ``speedup``: simulated seconds per wall second. ``start_delay_s``: simulated seconds in
    ``standby`` before the print starts. ``eventtime`` = ``eventtime_offset`` + simulated seconds
    since the server started.
    """

    def __init__(self, scenario: Scenario | str = "slow_viscous", speedup: float = 60.0,
                 port: int = 0, host: str = "127.0.0.1", start_delay_s: float = 5.0,
                 purge_macro: str = "CLOGGUARD_PURGE", eventtime_offset: float = 1000.0,
                 max_temp_c: float = 250.0, purge_timeout_s: float = 60.0,
                 params: PhysicsParams | None = None) -> None:
        self.scenario = preset(scenario) if isinstance(scenario, str) else scenario
        if speedup <= 0:
            raise ValueError("speedup must be positive")
        self.speedup = float(speedup)
        self.host = host
        self.port = port
        self.start_delay_s = start_delay_s
        self.purge_macro = purge_macro
        self.eventtime_offset = eventtime_offset
        self.max_temp_c = max_temp_c
        self.purge_timeout_s = purge_timeout_s
        self.sim = Simulator(self.scenario, params)
        self.clock = 0.0  # simulated seconds since server start
        self.print_duration = 0.0
        self.total_duration = 0.0
        self.command_log: list[dict[str, Any]] = []
        self._clients: set[_Client] = set()
        self._server: Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._force_window: deque[float] = deque(maxlen=10)  # 1 s average like load_cell
        self._status: dict[str, dict[str, Any]] = {}
        self._tick_event = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------------
    async def __aenter__(self) -> MockMoonraker:
        self._status = self._build_status()
        self._server = await serve(self._handler, self.host, self.port, max_size=2**22)
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]
        self._task = asyncio.create_task(self._sim_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/websocket"

    @property
    def eventtime(self) -> float:
        return round(self.eventtime_offset + self.clock, 4)

    @property
    def state(self) -> str:
        return self.sim.state

    def run_frame(self) -> pd.DataFrame:
        """Ground truth recorded so far (every channel incl. labels; ``t`` from print start)."""
        return self.sim.frame()

    async def wait_for(self, predicate: Any, timeout: float = 30.0) -> None:
        """Wait (wall seconds) until ``predicate(self)`` is true; checked every sim tick."""
        deadline = time.monotonic() + timeout
        while not predicate(self):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("condition not reached")
            ev = self._tick_event
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ev.wait(), min(remaining, 0.5))

    async def wait_for_state(self, state: str, timeout: float = 30.0) -> None:
        await self.wait_for(lambda m: m.sim.state == state, timeout)

    # -- simulation loop ---------------------------------------------------------------------
    async def _sim_loop(self) -> None:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        sleep = min(DT_S / self.speedup, 0.01)
        while True:
            target = (loop.time() - t0) * self.speedup
            n = 0
            while self.clock + DT_S <= target + 1e-9:
                self._tick()
                n += 1
                if n % 25 == 0:
                    await asyncio.sleep(0)
            await asyncio.sleep(sleep)

    def _tick(self) -> None:
        self.clock = round(self.clock + DT_S, 6)
        if self.sim.state == "standby" and self.clock >= self.start_delay_s:
            self.sim.start()
        if self.sim.state in ("printing", "paused"):
            self.total_duration += DT_S
            if self.sim.state == "printing":
                self.print_duration += DT_S
        self.sim.step()
        self._force_window.append(self.sim.last["force_g"])
        self._status = self._build_status()
        self._broadcast()
        self._tick_event.set()  # wake every waiter, then arm a fresh event for the next tick
        self._tick_event = asyncio.Event()

    def _build_status(self) -> dict[str, dict[str, Any]]:
        sim, row = self.sim, self.sim.last
        z = _r(row["z_mm"], 3)
        e_pos = _r(sim.e_position_mm, 3)
        pos = [100.0, 100.0, z, e_pos]  # ASSUMPTION: x/y not simulated; fixed bed centre
        layer = int(row["layer"])
        force_avg = sum(self._force_window) / len(self._force_window) if self._force_window else 0.0
        sg = int(row["tmc_sg_result"])
        temp = row["extruder_temp_c"]
        return {
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "print_stats": {
                "state": sim.state,
                "filename": f"{self.scenario.geometry}_s{self.scenario.seed}.gcode",
                "print_duration": _r(self.print_duration, 1),
                "total_duration": _r(self.total_duration, 1),
                "filament_used": e_pos,
                "message": "",
                "info": {"current_layer": layer if layer > 0 else None,
                         "total_layer": sim.job.n_layers},
            },
            "pause_resume": {"is_paused": sim.state == "paused"},
            "extruder": {
                "temperature": _r(temp, 2),
                "target": _r(sim.target_c, 1),
                "power": _r(row["extruder_power"], 3),
                "can_extrude": temp >= 170.0,
            },
            "motion_report": {
                "live_extruder_velocity": _r(row["e_velocity_mm_s"]),
                "live_velocity": _r(row["toolhead_velocity_mm_s"], 3),
                "live_position": pos,
            },
            "gcode_move": {
                "speed_factor": _r(sim.speed_factor),
                "extrude_factor": _r(sim.extrude_factor),
                "speed": _r(row["toolhead_velocity_mm_s"] * 60.0, 1),
            },
            "toolhead": {"position": pos, "status": "Ready"},
            "fan": {"speed": _r(row["fan_speed"], 3)},
            "tmc2209 extruder": {
                "drv_status": {"sg_result": sg, "cs_actual": 20,
                               "stst": abs(row["e_velocity_mm_s"]) < 1e-3},
                "run_current": 0.6,
                "temperature": None,
            },
            "load_cell": {"force_g": _r(force_avg, 1), "is_calibrated": True,
                          "sample_rate": 80},
            "clogguard_sim": {
                "force_g": _r(row["force_g"], 1),
                "accel_rms": _r(row["accel_rms"]),
                "encoder_slip_mm": _r(row["encoder_slip_mm"]),
            },
        }

    def _select(self, objects: dict[str, list[str] | None]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, fields in objects.items():
            obj = self._status.get(name)
            if obj is None:
                continue  # Moonraker omits unknown objects
            out[name] = dict(obj) if fields is None else {f: obj[f] for f in fields if f in obj}
        return out

    def _broadcast(self) -> None:
        et = self.eventtime
        for client in list(self._clients):
            if not client.subscription:
                continue
            cur = self._select(client.subscription)
            diff: dict[str, dict[str, Any]] = {}
            for name, fields in cur.items():
                prev = client.last_sent.get(name, {})
                changed = {k: v for k, v in fields.items() if prev.get(k) != v}
                if changed:
                    diff[name] = changed
                    prev.update(changed)
                    client.last_sent[name] = prev
            if diff:
                client.queue.put_nowait(_dumps({"jsonrpc": "2.0",
                                                "method": "notify_status_update",
                                                "params": [diff, et]}))

    # -- connection handling -----------------------------------------------------------------
    async def _handler(self, ws: ServerConnection) -> None:
        client = _Client(ws)
        self._clients.add(client)
        writer = asyncio.create_task(self._writer(client))
        client.queue.put_nowait(_dumps({"jsonrpc": "2.0", "method": "notify_klippy_ready",
                                        "params": []}))
        try:
            async for raw in ws:
                reply = await self._dispatch(client, raw)
                if reply is not None:
                    client.queue.put_nowait(_dumps(reply))
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(client)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    async def _writer(self, client: _Client) -> None:
        try:
            while True:
                msg = await client.queue.get()
                await client.ws.send(msg)
        except ConnectionClosed:
            return

    async def _dispatch(self, client: _Client, raw: str | bytes) -> dict[str, Any] | None:
        try:
            req = json.loads(raw)
        except (ValueError, TypeError):
            return _error(None, ERR_PARSE, "Parse error")
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or "method" not in req:
            return _error(req.get("id") if isinstance(req, dict) else None, ERR_INVALID,
                          "Invalid Request")
        rid = req.get("id")
        method = str(req["method"])
        params = req.get("params") or {}
        entry: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": rid,
            "connection_id": client.connection_id,
            "sim_time_s": round(self.sim.print_t, 3),
            "clock_s": self.clock,
            "eventtime": self.eventtime,
            "wall_time": time.time(),
        }
        self.command_log.append(entry)
        try:
            result = await self._call(client, method, params)
        except RpcError as e:
            entry.update(ok=False, error=e.message)
            return None if rid is None else _error(rid, e.code, e.message)
        entry.update(ok=True, result=result if isinstance(result, str) else None,
                     done_wall_time=time.time(), done_sim_time_s=round(self.sim.print_t, 3))
        return None if rid is None else {"jsonrpc": "2.0", "result": result, "id": rid}

    async def _call(self, client: _Client, method: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise RpcError(ERR_PARAMS, "params must be an object")
        if method == "server.connection.identify":
            client.identified = dict(params)
            return {"connection_id": client.connection_id}
        if method == "server.info":
            return {"klippy_connected": True, "klippy_state": "ready",
                    "moonraker_version": "clogguard-mock", "components": []}
        if method == "printer.info":
            return {"state": "ready", "state_message": "Printer is ready",
                    "hostname": "clogguard-sim", "software_version": "clogguard-mock"}
        if method == "printer.objects.list":
            return {"objects": list(OBJECTS)}
        if method in ("printer.objects.query", "printer.objects.subscribe"):
            objects = params.get("objects")
            if not isinstance(objects, dict):
                raise RpcError(ERR_PARAMS, "objects must be a mapping")
            objs = {str(k): (None if v is None else [str(f) for f in v])
                    for k, v in objects.items()}
            status = self._select(objs)
            if method == "printer.objects.subscribe":
                client.subscription = objs
                client.last_sent = {k: dict(v) for k, v in status.items()}
            return {"eventtime": self.eventtime, "status": status}
        if method == "printer.gcode.script":
            return await self._gcode(str(params.get("script", "")))
        if method == "printer.print.pause":
            if self.sim.state not in ("printing", "paused"):
                raise RpcError(ERR_KLIPPY, "Print is not active")
            self.sim.pause()
            return "ok"
        if method == "printer.print.resume":
            if self.sim.state not in ("printing", "paused"):
                raise RpcError(ERR_KLIPPY, "Print is not active")
            self.sim.resume()
            return "ok"
        if method == "printer.print.cancel":
            if self.sim.state not in ("printing", "paused"):
                raise RpcError(ERR_KLIPPY, "Print is not active")
            self.sim.cancel()
            return "ok"
        raise RpcError(ERR_METHOD, f"Method not found: {method}")

    async def _gcode(self, script: str) -> str:
        line = script.strip()
        if m := _RE_M220.match(line):
            pct = int(m.group(1))
            if pct < 1:
                raise RpcError(ERR_KLIPPY, "M220 S must be positive")
            self.sim.set_speed_factor(pct / 100.0)
            return "ok"
        if m := _RE_TEMP.match(line):
            target = float(m.group(1))
            if target > self.max_temp_c:
                raise RpcError(ERR_KLIPPY, f"Requested temperature ({target:.1f}) out of range "
                                           f"(0.0:{self.max_temp_c:.1f})")
            self.sim.set_target(target)
            return "ok"
        if line == self.purge_macro:
            if self.sim.state not in ("printing", "paused"):
                raise RpcError(ERR_KLIPPY, "Purge needs an active print")
            self.sim.purge()
            # Moonraker answers "ok" only when the macro has completed.
            await self.wait_for(lambda m: m.sim.purge_remaining_s <= 0.0 or m.sim.done,
                                timeout=self.purge_timeout_s)
            return "ok"
        raise RpcError(ERR_KLIPPY, f"Unknown command:\"{line.split(' ')[0] if line else ''}\"")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": rid}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clogguard mock-printer",
                                 description="Mock Moonraker server running a SIMULATED print.")
    ap.add_argument("--scenario", default="slow_viscous", choices=sorted(PRESETS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--speedup", type=float, default=60.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7125)
    ap.add_argument("--start-delay", type=float, default=5.0, help="sim seconds in standby")
    ap.add_argument("--purge-macro", default="CLOGGUARD_PURGE")
    ap.add_argument("--exit-on-complete", action="store_true")
    ns = ap.parse_args(argv)

    async def run() -> None:
        sc = preset(ns.scenario, seed=ns.seed)
        async with MockMoonraker(sc, speedup=ns.speedup, port=ns.port, host=ns.host,
                                 start_delay_s=ns.start_delay,
                                 purge_macro=ns.purge_macro) as mock:
            print(f"mock Moonraker (SIMULATED {sc.name}, seed {sc.seed}, {ns.speedup:g}x) "
                  f"at {mock.url}", flush=True)
            if ns.exit_on_complete:
                await mock.wait_for(lambda m: m.sim.done, timeout=float("inf"))
            else:
                await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0
