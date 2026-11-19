"""Serial channels: a line protocol from an external microcontroller.

Each line is one JSON object: ``{"t_mcu": <seconds on the MCU clock>, "<key>": value, ...}``.
A channel reads ``params.key`` (default: its own name) from the lines of ``params.port``.
Several channels may share one port. A value may be a list: a burst of raw samples, evenly
spaced at ``params.raw_rate_hz`` and ending at ``t_mcu``.

The line reader is injectable (``SourceContext.line_reader_factory``: ``(port, baud) ->
AsyncIterator[str | bytes]``); the default opens a real port through the optional ``pyserial``
package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from clogguard.channels.base import (
    ClockAligner,
    SampleSink,
    SourceContext,
    register_source,
    to_float,
)
from clogguard.core.channels import ChannelSpec, SourceKind

log = logging.getLogger(__name__)

LineReaderFactory = Callable[[str, int], AsyncIterator[str | bytes]]
DEFAULT_BAUD = 250_000


async def pyserial_lines(port: str, baud: int) -> AsyncIterator[bytes]:
    """Read lines from a real serial port. Needs the optional ``pyserial`` package."""
    try:
        import serial  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "channel source 'serial' needs the optional 'pyserial' package to open "
            f"{port}: pip install pyserial"
        ) from exc
    ser = serial.Serial(port, baud, timeout=1.0)
    try:
        while True:
            line = await asyncio.to_thread(ser.readline)
            if line:
                yield line
    finally:
        ser.close()


@register_source(SourceKind.SERIAL)
class SerialSource:
    kind = SourceKind.SERIAL

    def __init__(self, specs: list[ChannelSpec], context: SourceContext | None = None) -> None:
        ctx = context or SourceContext()
        self.specs = list(specs)
        self._factory: LineReaderFactory = ctx.line_reader_factory or pyserial_lines
        self._clock = ctx.clock or time.monotonic
        # (port, baud) -> {line key: channel spec}
        self.ports: dict[tuple[str, int], dict[str, ChannelSpec]] = {}
        for s in specs:
            port = s.params.get("port")
            if not port:
                raise ValueError(f"serial channel {s.name!r} needs params.port")
            key = str(s.params.get("key", s.name))
            group = self.ports.setdefault((str(port), int(s.params.get("baud", DEFAULT_BAUD))), {})
            group[key] = s
        self._aligners = {p: ClockAligner() for p in self.ports}
        self.bad_lines = 0

    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def parse_line(
        self, line: str | bytes, port: tuple[str, int], host_now: float | None = None
    ) -> list[tuple[str, float, float]]:
        """Parse one line into ``(channel, value, host_time)`` samples. Bad lines -> []."""
        now = self._clock() if host_now is None else host_now
        try:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            text = text.strip()
            if not text:
                return []
            obj: Any = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError:
            self.bad_lines += 1
            return []
        t_mcu = to_float(obj.get("t_mcu"))
        # ASSUMPTION: lines without t_mcu are stamped with the host receive time.
        t = self._aligners[port].to_host(t_mcu, now) if t_mcu is not None else now
        out: list[tuple[str, float, float]] = []
        for key, spec in self.ports[port].items():
            if key not in obj:
                continue
            raw = obj[key]
            if isinstance(raw, list):
                rate = float(spec.params.get("raw_rate_hz", spec.rate_hz))
                n = len(raw)
                for i, item in enumerate(raw):
                    v = to_float(item)
                    if v is not None:
                        out.append((spec.name, v, t - (n - 1 - i) / rate))
            else:
                v = to_float(raw)
                if v is not None:
                    out.append((spec.name, v, t))
        return out

    async def run(self, sink: SampleSink) -> None:
        """Read every port until its stream ends (or the task is cancelled)."""
        await asyncio.gather(*(self._read_port(p, sink) for p in self.ports))

    async def _read_port(self, port: tuple[str, int], sink: SampleSink) -> None:
        async for line in self._factory(*port):
            for name, value, t in self.parse_line(line, port):
                sink(name, value, t)
        log.info("serial stream %s ended", port[0])
