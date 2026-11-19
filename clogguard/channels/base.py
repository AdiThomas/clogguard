"""Channel-source plugin registry.

Each ``SourceKind`` is implemented by one plugin class registered with ``@register_source``.
Adding a sensor is a config change; adding a new *kind* of source is one new plugin here.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from clogguard.core.channels import ChannelConfig, ChannelSpec, SourceKind
from clogguard.core.config import MaterialProfile

# A sink receives one sample: (channel name, value, host time in seconds).
SampleSink = Callable[[str, float, float], None]


@runtime_checkable
class ChannelSource(Protocol):
    """A group of channels sharing one source kind.

    Capabilities beyond this are kind-specific (documented on each plugin): Moonraker maps a
    merged status dict (``extract``), serial pushes samples into a sink (``run``), replay
    looks values up in a stored run (``values_at``), derived computes from other channels
    (``compute``).
    """

    kind: SourceKind
    specs: list[ChannelSpec]

    def names(self) -> list[str]: ...


@dataclass
class SourceContext:
    """Everything a plugin may need at construction. Unused fields are ignored."""

    material: MaterialProfile | None = None
    run: Any = None  # clogguard.core.schema.Run or a DataFrame, for replay
    line_reader_factory: Any = None  # serial: (port, baud) -> AsyncIterator[str | bytes]
    clock: Callable[[], float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


SourceFactory = Callable[[list[ChannelSpec], SourceContext], ChannelSource]
_REGISTRY: dict[SourceKind, SourceFactory] = {}


def register_source(kind: SourceKind) -> Callable[[SourceFactory], SourceFactory]:
    """Class decorator registering a plugin for ``kind`` (constructor ``(specs, context)``)."""

    def deco(factory: SourceFactory) -> SourceFactory:
        _REGISTRY[kind] = factory
        return factory

    return deco


def registered_kinds() -> list[SourceKind]:
    return sorted(_REGISTRY)


def build_sources(
    channels: ChannelConfig | Iterable[ChannelSpec],
    context: SourceContext | None = None,
    *,
    skip_unregistered: bool = False,
) -> dict[SourceKind, ChannelSource]:
    """Group channels by source kind and instantiate one plugin per kind."""
    specs = channels.channels if isinstance(channels, ChannelConfig) else list(channels)
    ctx = context or SourceContext()
    groups: dict[SourceKind, list[ChannelSpec]] = {}
    for s in specs:
        groups.setdefault(s.source, []).append(s)
    out: dict[SourceKind, ChannelSource] = {}
    for kind, group in groups.items():
        factory = _REGISTRY.get(kind)
        if factory is None:
            if skip_unregistered:
                continue
            raise KeyError(
                f"no channel-source plugin registered for {kind.value!r} "
                f"(channels {[s.name for s in group]}); registered: "
                f"{[k.value for k in registered_kinds()]}"
            )
        out[kind] = factory(group, ctx)
    return out


def to_float(value: Any) -> float | None:
    """Coerce a status value to float; None for missing or non-numeric values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class ClockAligner:
    """Map a remote clock (MCU ``t_mcu``, Moonraker ``eventtime``) onto the host clock.

    offset = min over a sliding window of (host_receive_time - remote_time). The minimum is the
    sample with the least transport latency, so remote events are placed at their true time
    rather than their arrival time; the window lets the estimate follow slow crystal drift.
    """

    def __init__(self, window: int = 256) -> None:
        self._offsets: deque[float] = deque(maxlen=window)

    def to_host(self, remote_t: float, host_now: float) -> float:
        self._offsets.append(host_now - remote_t)
        return remote_t + min(self._offsets)

    @property
    def offset(self) -> float | None:
        return min(self._offsets) if self._offsets else None
