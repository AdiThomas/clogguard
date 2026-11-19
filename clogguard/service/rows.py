"""A small base-rate row builder: Moonraker status diffs -> rows of channel values.

This is deliberately minimal. It only serves channels with ``source: moonraker`` (``params``
``object``, ``field``, optional ``index`` into a list and ``key`` into a dict) and the derived
``volumetric_flow`` function. A fuller sampler from ``clogguard.channels`` can replace it by
implementing the ``RowSource`` protocol.

Rows are on a ``BASE_RATE_HZ`` grid of ``t`` (seconds since print start) with zero-order hold:
each row carries the latest value known at that time. Holding stops ``stale_after_s`` after the
last status update, so a long gap produces no invented rows.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

import pandas as pd

from clogguard.core.channels import ChannelConfig, ChannelSpec, SourceKind
from clogguard.core.schema import BASE_RATE_HZ

# Objects the runtime always subscribes to for print state, besides channel objects.
STATE_OBJECTS: dict[str, list[str] | None] = {
    "print_stats": None,
    "pause_resume": None,
    "webhooks": None,
}


class RowSource(Protocol):
    def update(self, diff: dict[str, Any], eventtime: float) -> None: ...

    def take_rows(self, t_s: float) -> pd.DataFrame: ...

    def freshness(self, t_s: float) -> tuple[bool, str]: ...


def subscription_objects(channels: ChannelConfig) -> dict[str, list[str] | None]:
    """The ``printer.objects.subscribe`` objects needed for the moonraker channels."""
    objs: dict[str, list[str] | None] = {}
    for spec in channels.channels:
        if spec.source is not SourceKind.MOONRAKER or "object" not in spec.params:
            continue
        obj, fld = str(spec.params["object"]), spec.params.get("field")
        if obj in objs and objs[obj] is None:
            continue  # already subscribed to every field
        if fld is None:
            objs[obj] = None
            continue
        fields = list(objs.get(obj) or [])
        if str(fld) not in fields:
            fields.append(str(fld))
        objs[obj] = fields
    for obj, fields in STATE_OBJECTS.items():
        objs[obj] = fields
    return objs


def extract(obj_status: Any, params: dict[str, Any]) -> float:
    """One channel value from one object's status; NaN when absent or not numeric."""
    try:
        v = obj_status[params["field"]]
        if params.get("index") is not None:
            v = v[int(params["index"])]
        if params.get("key") is not None:
            v = v[params["key"]]
    except (KeyError, IndexError, TypeError, ValueError):
        return math.nan
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float):
        return float(v)
    return math.nan


class StatusRowBuilder:
    def __init__(
        self,
        channels: ChannelConfig,
        *,
        start_eventtime: float,
        filament_area_mm2: float,
        stale_after_s: float,
    ) -> None:
        self._start = start_eventtime
        self._area = filament_area_mm2
        self._stale = stale_after_s
        self._mr: list[ChannelSpec] = [
            c for c in channels.channels
            if c.source is SourceKind.MOONRAKER and "object" in c.params and "field" in c.params
        ]
        self._derived: list[ChannelSpec] = [
            c for c in channels.channels
            if c.source is SourceKind.DERIVED and c.params.get("fn") == "volumetric_flow"
        ]
        self._required = [c.name for c in self._mr if c.role == "signal"]
        self.columns = [c.name for c in self._mr] + [c.name for c in self._derived]
        self.values: dict[str, float] = {n: math.nan for n in self.columns}
        self.updated_at: dict[str, float | None] = {n: None for n in self.columns}
        self._last_update_t: float | None = None
        self._step = 1.0 / BASE_RATE_HZ
        self._k = 0  # next grid index
        self._rows: list[list[float]] = []

    @property
    def last_update_t(self) -> float | None:
        """Print time of the most recent status update, if any."""
        return self._last_update_t

    def t_of(self, eventtime: float) -> float:
        return eventtime - self._start

    def age(self, name: str, t_s: float) -> float:
        """Seconds since ``name`` last changed (inf if never). Klipper only reports changes, so
        a large age can also mean a constant value."""
        u = self.updated_at[name]
        return math.inf if u is None else t_s - u

    def _emit_until(self, t_s: float, inclusive: bool) -> None:
        last = self._last_update_t
        if last is None:
            self._k = max(self._k, math.ceil(t_s / self._step - 1e-9))
            return
        while True:
            tk = self._k * self._step
            if tk > t_s + 1e-9 or (abs(tk - t_s) <= 1e-9 and not inclusive):
                return
            if tk - last > self._stale:  # no invented rows across a gap
                self._k = max(self._k + 1, math.ceil(t_s / self._step - 1e-9))
                continue
            if tk >= 0:
                self._rows.append([round(tk, 6), *(self.values[n] for n in self.columns)])
            self._k += 1

    def update(self, diff: dict[str, Any], eventtime: float) -> None:
        t = self.t_of(eventtime)
        self._emit_until(t, inclusive=False)
        for spec in self._mr:
            obj = diff.get(spec.params["object"])
            if isinstance(obj, dict) and spec.params["field"] in obj:
                self.values[spec.name] = extract(obj, spec.params)
                self.updated_at[spec.name] = t
        for spec in self._derived:
            inputs = spec.params.get("inputs") or []
            src = self.values.get(inputs[0], math.nan) if inputs else math.nan
            self.values[spec.name] = src * self._area
            self.updated_at[spec.name] = t
        self._last_update_t = t

    def take_rows(self, t_s: float) -> pd.DataFrame:
        self._emit_until(t_s, inclusive=True)
        rows, self._rows = self._rows, []
        return pd.DataFrame(rows, columns=["t", *self.columns])

    def freshness(self, t_s: float) -> tuple[bool, str]:
        if self._last_update_t is None:
            return False, "no printer status yet"
        if t_s - self._last_update_t > self._stale:
            return False, f"no status update for {t_s - self._last_update_t:.1f}s"
        missing = [n for n in self._required if math.isnan(self.values[n])]
        if missing:
            return False, f"missing signal channels: {missing}"
        return True, ""
