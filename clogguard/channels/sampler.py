"""Resample asynchronous channel samples onto the base-rate grid.

Slow channels (``rate_hz <= base rate``) are zero-order held: each row carries the latest value.
Fast channels (``rate_hz > base rate`` or ``params.raw_rate_hz`` set) are averaged over the
samples that fall inside the tick; if a tick receives none the previous value is held and the
channel's age shows it. Derived channels are computed from each finished row.

Times passed to ``push`` and ``tick`` are host seconds (any monotonic clock). ``origin`` is
subtracted to give the row's ``t`` (seconds since print start).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from clogguard.channels.derived import DerivedSource
from clogguard.core.channels import ChannelSpec, SourceKind
from clogguard.core.schema import BASE_RATE_HZ


def is_fast(spec: ChannelSpec, base_rate_hz: float = BASE_RATE_HZ) -> bool:
    return spec.rate_hz > base_rate_hz or "raw_rate_hz" in spec.params


class RawBuffer:
    """Full-rate samples of fast channels, drained periodically to ``raw/<channel>.parquet``."""

    def __init__(self, channels: Iterable[str]) -> None:
        self._t: dict[str, list[float]] = {c: [] for c in channels}
        self._v: dict[str, list[float]] = {c: [] for c in channels}

    @property
    def channels(self) -> list[str]:
        return list(self._t)

    def append(self, name: str, t: float, value: float) -> None:
        self._t[name].append(t)
        self._v[name].append(value)

    def __len__(self) -> int:
        return sum(len(v) for v in self._t.values())

    def drain(self) -> dict[str, pd.DataFrame]:
        """Return and clear buffered samples per channel (columns ``t``, ``value``)."""
        out: dict[str, pd.DataFrame] = {}
        for c in self._t:
            if self._t[c]:
                out[c] = pd.DataFrame({"t": self._t[c], "value": self._v[c]})
                self._t[c], self._v[c] = [], []
        return out


class Sampler:
    def __init__(
        self,
        specs: Iterable[ChannelSpec],
        *,
        base_rate_hz: float = BASE_RATE_HZ,
        derived: DerivedSource | None = None,
        keep_raw: bool = False,
    ) -> None:
        self.base_rate_hz = base_rate_hz
        all_specs = list(specs)
        self.derived = derived
        derived_names = set(derived.names()) if derived else set()
        self.specs = [s for s in all_specs if s.source is not SourceKind.DERIVED]
        self.derived_names = [s.name for s in all_specs if s.name in derived_names]
        self.fast = {s.name for s in self.specs if is_fast(s, base_rate_hz)}
        self._known = {s.name for s in self.specs}
        self.origin = 0.0
        self._value: dict[str, float] = {}
        self._time: dict[str, float] = {}
        self._acc: dict[str, list[tuple[float, float]]] = {n: [] for n in self.fast}
        self.raw = RawBuffer(sorted(self.fast)) if keep_raw and self.fast else None

    @property
    def columns(self) -> list[str]:
        return [s.name for s in self.specs] + self.derived_names

    def reset(self, origin: float) -> None:
        """Start a new run at host time ``origin``. Latest values are kept (they are still the
        truth); pending fast samples and raw data are discarded."""
        self.origin = origin
        for acc in self._acc.values():
            acc.clear()
        if self.raw is not None:
            self.raw.drain()

    def push(self, name: str, value: float | None, t: float) -> None:
        """Record one sample. ``None``/NaN values are ignored (the channel just ages)."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        if name in self._acc:
            self._acc[name].append((t, float(value)))
            if self.raw is not None:
                self.raw.append(name, t - self.origin, float(value))
        elif name not in self._known:
            raise KeyError(f"unknown channel {name!r}")
        else:
            self._value[name] = float(value)
        prev = self._time.get(name)
        if prev is None or t > prev:
            self._time[name] = t

    def push_many(self, values: Mapping[str, float | None], t: float) -> None:
        for name, value in values.items():
            self.push(name, value, t)

    def tick(self, now: float) -> dict[str, float]:
        """Emit one row for host time ``now``: ``{"t": now - origin, channel: value, ...}``.
        Missing values are NaN."""
        for name, acc in self._acc.items():
            inside = [v for (ts, v) in acc if ts <= now]
            if inside:
                self._value[name] = float(np.mean(inside))
                acc[:] = [(ts, v) for (ts, v) in acc if ts > now]
        row: dict[str, float] = {"t": now - self.origin}
        for s in self.specs:
            row[s.name] = self._value.get(s.name, math.nan)
        if self.derived is not None:
            for name, v in self.derived.compute(row).items():
                row[name] = math.nan if v is None else float(v)
        return row

    def age(self, name: str, now: float) -> float | None:
        """Seconds since the channel's newest sample; None if never seen. Derived channels
        take the age of their oldest input."""
        if self.derived is not None and name in self.derived_names:
            ages = [self.age(i, now) for i in self.derived.inputs(name)]
            return None if any(a is None for a in ages) else max(a for a in ages if a is not None)
        t = self._time.get(name)
        return None if t is None else now - t

    def ages(self, now: float) -> dict[str, float | None]:
        return {n: self.age(n, now) for n in self.columns}

    def stale(self, now: float, max_age_s: float) -> list[str]:
        """Channels never seen or older than ``max_age_s``."""
        return [n for n, a in self.ages(now).items() if a is None or a > max_age_s]
