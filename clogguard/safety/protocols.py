"""The two outbound interfaces of the service.

``PrinterLink`` is the only way to reach the printer. By project rule only ``clogguard.safety``
may call ``PrinterLink.call``; everything else may only subscribe and read updates. A source
scan test (``tests/safety/test_source_scan.py``) enforces the rule.

``Notifier`` is where ``Notify`` commands go (dashboard, phone). It never reaches the printer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PrinterLink(Protocol):
    """A connection to Moonraker (or a fake of it)."""

    @property
    def connected(self) -> bool:
        """True while the transport is up."""
        ...

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        """Send one JSON-RPC request and return its result once the printer acknowledges it.
        Raises on a JSON-RPC error or a transport failure. Only ``clogguard.safety`` calls this.
        """
        ...

    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]:
        """Subscribe to printer objects. Returns ``{"eventtime": float, "status": {...}}``."""
        ...

    def updates(self) -> AsyncIterator[tuple[dict[str, Any], float]]:
        """Async iterator of ``(status_diff, eventtime)``. Ends when the link disconnects."""
        ...


@runtime_checkable
class Notifier(Protocol):
    """Receives user notifications. Must never talk to the printer."""

    async def notify(self, level: str, message: str) -> None: ...
