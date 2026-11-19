"""A tiny in-test Moonraker: JSON-RPC over websockets following /tmp/wire.md."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from clogguard.moonraker.status import merge_status


class FakeMoonraker:
    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status: dict[str, Any] = status or {}
        self.eventtime = 1000.0
        self.requests: list[dict[str, Any]] = []
        self.identify_params: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.conns: set[ServerConnection] = set()
        self.server: Server | None = None
        self.url = ""
        self._next_conn = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> str:
        self.server = await serve(self._handler, "127.0.0.1", 0, compression=None)
        port = next(iter(self.server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/websocket"
        return self.url

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def drop_all(self) -> None:
        for c in list(self.conns):
            await c.close()

    async def wait_conns(self, n: int, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.conns) < n:
                await asyncio.sleep(0.01)

    async def _handler(self, ws: ServerConnection) -> None:
        self.conns.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.requests.append(msg)
                task = asyncio.create_task(self._respond(ws, msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            pass
        finally:
            self.conns.discard(ws)

    async def _send(self, ws: ServerConnection, obj: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(obj))

    async def _respond(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        method, params, rid = msg["method"], msg.get("params", {}), msg["id"]
        result: Any = None
        if method == "server.connection.identify":
            self._next_conn += 1
            self.identify_params.append(params)
            result = {"connection_id": self._next_conn}
        elif method in ("printer.objects.subscribe", "printer.objects.query"):
            objs = params["objects"]
            if method.endswith("subscribe"):
                self.subscriptions.append(objs)
            result = {"eventtime": self.eventtime, "status": self._filtered(objs)}
        elif method == "test.delay":
            await asyncio.sleep(params["delay"])
            result = params["value"]
        elif method == "test.never":
            return
        elif method == "printer.gcode.script":
            result = "ok"
        else:
            await self._send(
                ws,
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": rid,
                },
            )
            return
        await self._send(ws, {"jsonrpc": "2.0", "result": result, "id": rid})

    def _filtered(self, objs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, fields in objs.items():
            obj = self.status.get(name, {})
            out[name] = dict(obj) if fields is None else {f: obj[f] for f in fields if f in obj}
        return json.loads(json.dumps(out))

    async def notify(self, diff: dict[str, Any], dt: float = 0.1) -> None:
        self.eventtime += dt
        merge_status(self.status, diff)
        msg = {"jsonrpc": "2.0", "method": "notify_status_update", "params": [diff, self.eventtime]}
        for c in list(self.conns):
            await self._send(c, msg)

    async def klippy(self, method: str) -> None:
        for c in list(self.conns):
            await self._send(c, {"jsonrpc": "2.0", "method": method, "params": []})
