"""Async JSON-RPC 2.0 client for Moonraker's websocket API.

Generic ``call`` only. This module deliberately has no G-code helpers: ``clogguard.safety`` is the
only code allowed to build printer commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from clogguard import __version__

log = logging.getLogger(__name__)

KLIPPY_NOTIFICATIONS: dict[str, str] = {
    "notify_klippy_ready": "ready",
    "notify_klippy_shutdown": "shutdown",
    "notify_klippy_disconnected": "disconnected",
}

_CLOSED = object()


class MoonrakerError(Exception):
    """A JSON-RPC error returned by Moonraker, or a transport problem (see subclasses)."""

    def __init__(self, message: str, code: int | None = None, method: str | None = None):
        super().__init__(message if code is None else f"{message} (code {code})")
        self.code = code
        self.message = message
        self.method = method


class MoonrakerConnectionError(MoonrakerError, ConnectionError):
    """Not connected, or the connection dropped while a request was in flight."""


class MoonrakerTimeoutError(MoonrakerError, TimeoutError):
    """No response within the request timeout."""


_OPEN_ERRORS = (OSError, InvalidHandshake, InvalidURI, TimeoutError, MoonrakerError)


class MoonrakerClient:
    """One websocket connection to Moonraker with id-matched requests, subscription
    notifications and automatic reconnect.

    Satisfies the service's printer-client protocol: ``call(method, params)``,
    ``subscribe(objects) -> initial status``, ``updates()`` yielding ``(status_diff, eventtime)``
    and a ``connected`` property.

    After a reconnect the last subscription is re-sent and its full status is pushed into
    ``updates()`` as one diff, so consumers that merge diffs resynchronise automatically.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        api_key_env: str | None = "MOONRAKER_API_KEY",
        client_name: str = "clogguard",
        version: str = __version__,
        request_timeout_s: float = 5.0,
        reconnect: bool = True,
        backoff_initial_s: float = 0.5,
        backoff_max_s: float = 30.0,
        open_timeout_s: float = 10.0,
        max_queued_updates: int = 10_000,
    ) -> None:
        self.url = url
        self.api_key_env = api_key_env  # name of the env var only; the key is never stored
        self.client_name = client_name
        self.version = version
        self.request_timeout_s = request_timeout_s
        self.reconnect = reconnect
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.open_timeout_s = open_timeout_s

        self.connection_id: int | None = None
        self.klippy_state: str | None = None
        self.reconnects = 0
        self.dropped_updates = 0
        self.last_eventtime: float | None = None

        self._ws: ClientConnection | None = None
        self._ready = False
        self._closing = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscription: dict[str, Any] | None = None
        self._updates: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queued_updates)
        self._klippy: asyncio.Queue[Any] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._klippy_callbacks: list[Callable[[str], None]] = []
        self._connected_event = asyncio.Event()

    # ------------------------------------------------------------------ properties
    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ready

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

    def on_klippy_state(self, callback: Callable[[str], None]) -> None:
        """Register a callback for klippy state changes (ready/shutdown/disconnected)."""
        self._klippy_callbacks.append(callback)

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, url: str | None = None, *, retry: bool = False) -> None:
        """Open the connection and identify. With ``retry`` keep trying with exponential
        backoff until it succeeds (or ``close`` is called); otherwise raise on failure."""
        if url is not None:
            self.url = url
        if not self.url:
            raise ValueError("no Moonraker URL given")
        self._closing = False
        delay = self.backoff_initial_s
        while True:
            try:
                await self._open()
                break
            except _OPEN_ERRORS as exc:
                if not retry or self._closing:
                    raise
                log.warning("moonraker connect failed (%s); retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff_max_s)
        self._supervisor = asyncio.create_task(self._supervise(), name="moonraker-supervisor")

    async def close(self) -> None:
        self._closing = True
        tasks = [t for t in (self._supervisor, self._reader) if t is not None]
        for t in tasks:
            t.cancel()
        ws, self._ws = self._ws, None
        self._ready = False
        self._connected_event.clear()
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._fail_pending(MoonrakerConnectionError("client closed"))
        self._put_update(_CLOSED, force=True)
        self._klippy.put_nowait(_CLOSED)

    async def __aenter__(self) -> MoonrakerClient:
        if not self.connected:
            await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _open(self) -> None:
        assert self.url is not None
        # proxy=None: Moonraker is on the LAN; never route it through an environment proxy.
        ws = await connect(
            self.url, proxy=None, open_timeout=self.open_timeout_s, max_size=None, compression=None
        )
        self._ws = ws
        self._reader = asyncio.create_task(self._read_loop(ws), name="moonraker-reader")
        try:
            ident = await self._call(ws, "server.connection.identify", self._identify_params())
            self.connection_id = ident.get("connection_id") if isinstance(ident, dict) else None
            if self._subscription is not None:
                result = await self._call(
                    ws, "printer.objects.subscribe", {"objects": self._subscription}
                )
                status, eventtime = _status_of(result)
                self._put_update((status, eventtime))
        except BaseException:
            self._ws = None
            self._reader.cancel()
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        self._ready = True
        self._connected_event.set()
        log.info("moonraker connected (connection_id=%s)", self.connection_id)

    def _identify_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "client_name": self.client_name,
            "version": self.version,
            "type": "agent",
            "url": "https://github.com/clogguard",
        }
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if key:
            params["api_key"] = key  # never logged: only method names are logged
        return params

    async def _supervise(self) -> None:
        while not self._closing:
            reader = self._reader
            if reader is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
            if self._closing:
                return
            self._ws = None
            self._ready = False
            self._connected_event.clear()
            self._fail_pending(MoonrakerConnectionError("connection lost"))
            log.warning("moonraker connection lost")
            if not self.reconnect:
                self._put_update(_CLOSED, force=True)
                self._klippy.put_nowait(_CLOSED)
                return
            delay = self.backoff_initial_s
            while not self._closing:
                await asyncio.sleep(delay)
                try:
                    await self._open()
                    self.reconnects += 1
                    break
                except _OPEN_ERRORS as exc:
                    log.warning("moonraker reconnect failed (%s); retry in %.1fs", exc, delay)
                    delay = min(delay * 2, self.backoff_max_s)

    # ------------------------------------------------------------------ messages
    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("moonraker sent non-JSON message; ignored")
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except ConnectionClosed:
            pass

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                fut.set_exception(
                    MoonrakerError(str(err.get("message", "unknown error")), err.get("code"))
                )
            else:
                fut.set_result(msg["result"])
            return
        method = msg.get("method")
        params = msg.get("params") or []
        if method == "notify_status_update":
            # ASSUMPTION: params are [status_diff, eventtime] (wire.md).
            diff = params[0] if params and isinstance(params[0], dict) else {}
            eventtime = (
                float(params[1])
                if len(params) > 1 and isinstance(params[1], int | float)
                else time.monotonic()
            )
            self.last_eventtime = eventtime
            self._put_update((diff, eventtime))
        elif method in KLIPPY_NOTIFICATIONS:
            state = KLIPPY_NOTIFICATIONS[method]
            self.klippy_state = state
            self._klippy.put_nowait(state)
            for cb in self._klippy_callbacks:
                try:
                    cb(state)
                except Exception:  # a bad callback must not kill the reader
                    log.exception("klippy state callback failed")

    def _put_update(self, item: Any, force: bool = False) -> None:
        while True:
            try:
                self._updates.put_nowait(item)
                return
            except asyncio.QueueFull:
                # Drop the oldest diff; consumers see a gap rather than unbounded memory.
                self._updates.get_nowait()
                self.dropped_updates += 1
                if not force:
                    log.warning("moonraker update queue full; dropped oldest diff")

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def _call(
        self,
        ws: ClientConnection,
        method: str,
        params: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> Any:
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id}
        log.debug("moonraker call %s id=%d", method, req_id)  # params never logged
        try:
            await ws.send(json.dumps(payload))
        except ConnectionClosed as exc:
            self._pending.pop(req_id, None)
            raise MoonrakerConnectionError("connection closed", method=method) from exc
        try:
            return await asyncio.wait_for(fut, timeout or self.request_timeout_s)
        except TimeoutError as exc:
            raise MoonrakerTimeoutError(f"{method} timed out", method=method) from exc
        except MoonrakerError as exc:
            exc.method = method
            raise
        finally:
            self._pending.pop(req_id, None)

    # ------------------------------------------------------------------ public API
    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        """Send one JSON-RPC request and return its ``result``.

        Raises ``MoonrakerError`` for a JSON-RPC error, ``MoonrakerTimeoutError`` after
        ``timeout`` seconds, ``MoonrakerConnectionError`` when not connected.
        """
        ws = self._ws
        if ws is None or not self._ready:
            raise MoonrakerConnectionError("not connected", method=method)
        return await self._call(ws, method, params, timeout)

    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
        """Subscribe to printer objects; returns the initial full status. Remembered and
        re-sent after every reconnect."""
        result = await self.call("printer.objects.subscribe", {"objects": objects})
        self._subscription = dict(objects)
        status, eventtime = _status_of(result)
        if not math.isnan(eventtime):
            self.last_eventtime = eventtime
        return status

    async def updates(self) -> AsyncIterator[tuple[dict[str, Any], float]]:
        """Yield ``(status_diff, eventtime)`` until the client is closed. Single consumer."""
        while True:
            if self._closing and self._updates.empty():
                return
            item = await self._updates.get()
            if item is _CLOSED:
                return
            yield item

    async def klippy_events(self) -> AsyncIterator[str]:
        """Yield klippy states ("ready", "shutdown", "disconnected") until closed."""
        while True:
            if self._closing and self._klippy.empty():
                return
            item = await self._klippy.get()
            if item is _CLOSED:
                return
            yield item


def _status_of(result: Any) -> tuple[dict[str, Any], float]:
    if not isinstance(result, dict):
        return {}, math.nan
    status = result.get("status") or {}
    et = result.get("eventtime")
    return status, float(et) if isinstance(et, int | float) else math.nan
