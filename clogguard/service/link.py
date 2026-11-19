"""PrinterLink implementations.

* ``MoonrakerLink`` - adapter around ``clogguard.moonraker.MoonrakerClient`` (workstream A).
  The client is imported lazily inside ``from_config`` so this module imports even when that
  package is absent. Client surface relied on: ``connect(retry=True)``, ``close()``,
  ``connected``, ``call(method, params)``, ``subscribe(objects) -> status``,
  ``last_eventtime`` and ``updates()`` yielding ``(status_diff, eventtime)``.
* ``InMemoryLink`` - fake for tests: records every call, lets tests push status diffs, delay
  or fail acks, and simulate disconnects.

Only ``clogguard.safety`` may call ``PrinterLink.call``. ``MoonrakerLink.call`` merely forwards
its arguments unchanged; the source-scan test checks that it does nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib
import math
from collections.abc import AsyncIterator
from typing import Any

from clogguard.core.config import ServiceConfig

_DISCONNECT = object()


class MoonrakerLink:
    def __init__(self, client: Any, *, poll_s: float = 0.25) -> None:
        self._client = client
        self._poll = poll_s
        self._connector: asyncio.Task[None] | None = None

    @classmethod
    def from_config(cls, config: ServiceConfig) -> MoonrakerLink:
        try:
            mod = importlib.import_module("clogguard.moonraker")
            client_cls = mod.MoonrakerClient
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "clogguard.moonraker.MoonrakerClient is not available; install the full "
                f"clogguard package ({exc})"
            ) from exc
        return cls(client_cls(config.moonraker_url, api_key_env=config.api_key_env))

    async def start(self) -> None:
        """Begin connecting in the background (retries with backoff until it succeeds)."""
        self._connector = asyncio.create_task(self._client.connect(retry=True))

    async def close(self) -> None:
        if self._connector is not None:
            self._connector.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._connector
        await self._client.close()

    @property
    def connected(self) -> bool:
        return bool(getattr(self._client, "connected", False))

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        return await self._client.call(method, params)

    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
        status = await self._client.subscribe(objects)
        et = getattr(self._client, "last_eventtime", None)
        return {"eventtime": math.nan if et is None else float(et), "status": status}

    async def updates(self) -> AsyncIterator[tuple[dict[str, Any], float]]:
        """The client's stream, ended when the client reports a lost connection (the client
        itself keeps one stream across reconnects; the runtime re-subscribes instead)."""
        it = self._client.updates().__aiter__()
        while True:
            nxt: asyncio.Future[Any] = asyncio.ensure_future(it.__anext__())
            while not nxt.done():
                await asyncio.wait({nxt}, timeout=self._poll)
                if not nxt.done() and not self.connected:
                    nxt.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await nxt
                    return
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            yield item


class InMemoryLink:
    """A fake printer link for tests.

    ``calls`` records every ``(method, params)``. ``ack_delay_s`` delays each ack;
    ``hang=True`` never acks; ``fail_with`` raises that exception from ``call``.
    ``push`` feeds a status diff; ``disconnect``/``reconnect`` simulate link loss.
    """

    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        eventtime: float = 0.0,
        connected: bool = True,
        ack_delay_s: float = 0.0,
        hang: bool = False,
        fail_with: BaseException | None = None,
    ) -> None:
        self.status: dict[str, Any] = copy.deepcopy(status or {})
        self.eventtime = eventtime
        self._connected = connected
        self.ack_delay_s = ack_delay_s
        self.hang = hang
        self.fail_with = fail_with
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._waiting = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if not self._connected:
            raise ConnectionError("not connected")
        self.calls.append((method, copy.deepcopy(params)))
        if self.fail_with is not None:
            raise self.fail_with
        if self.hang:
            await asyncio.Event().wait()
        if self.ack_delay_s:
            await asyncio.sleep(self.ack_delay_s)
        return "ok"

    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
        if not self._connected:
            raise ConnectionError("not connected")
        self.subscriptions.append(copy.deepcopy(objects))
        return {"eventtime": self.eventtime, "status": copy.deepcopy(self.status)}

    async def updates(self) -> AsyncIterator[tuple[dict[str, Any], float]]:
        while True:
            self._waiting = True
            item = await self._queue.get()
            self._waiting = False
            if item is _DISCONNECT:
                return
            yield item

    def push(self, diff: dict[str, Any], eventtime: float) -> None:
        for obj, fields in diff.items():
            self.status[obj] = {**self.status.get(obj, {}), **copy.deepcopy(fields)}
        self.eventtime = eventtime
        self._queue.put_nowait((copy.deepcopy(diff), eventtime))

    def disconnect(self) -> None:
        self._connected = False
        self._queue.put_nowait(_DISCONNECT)

    def reconnect(self) -> None:
        self._connected = True

    async def settle(self, timeout: float = 5.0) -> None:
        """Wait until every pushed item has been consumed and the consumer waits again."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not (self._queue.empty() and self._waiting):
            if loop.time() > end:
                raise TimeoutError("link consumer did not settle")
            await asyncio.sleep(0.001)
