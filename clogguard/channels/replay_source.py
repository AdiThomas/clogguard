"""Replay channels: columns of a stored run, for offline replays and tests.

Params: optional ``column`` (default: the channel name). Values are zero-order held between
stored rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd

from clogguard.channels.base import SourceContext, register_source
from clogguard.core.channels import ChannelSpec, SourceKind


@register_source(SourceKind.REPLAY)
class ReplaySource:
    kind = SourceKind.REPLAY

    def __init__(self, specs: list[ChannelSpec], context: SourceContext | None = None) -> None:
        ctx = context or SourceContext()
        if ctx.run is None:
            raise ValueError("replay channels need SourceContext.run (a Run or DataFrame)")
        frame: pd.DataFrame = ctx.run if isinstance(ctx.run, pd.DataFrame) else ctx.run.frame
        if "t" not in frame.columns:
            raise ValueError("replay frame has no 't' column")
        self.specs = list(specs)
        self._columns = {s.name: str(s.params.get("column", s.name)) for s in specs}
        missing = [c for c in self._columns.values() if c not in frame.columns]
        if missing:
            raise ValueError(f"replay frame lacks columns {missing}")
        self.frame = frame.sort_values("t").reset_index(drop=True)
        self._t = self.frame["t"].to_numpy(dtype=float)

    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def values_at(self, t: float) -> dict[str, float | None]:
        """Latest stored value at or before ``t`` (None before the first row)."""
        i = int(np.searchsorted(self._t, t, side="right")) - 1
        if i < 0:
            return dict.fromkeys(self._columns)
        row = self.frame.iloc[i]
        out: dict[str, float | None] = {}
        for name, col in self._columns.items():
            v: Any = row[col]
            out[name] = None if pd.isna(v) else float(v)
        return out

    def iter_samples(self) -> Iterator[tuple[float, dict[str, float | None]]]:
        """Yield ``(t, {channel: value})`` for every stored row in time order."""
        names = list(self._columns)
        values = self.frame[list(self._columns.values())].to_numpy(dtype=float)
        for t, row in zip(self._t, values, strict=True):
            yield (
                float(t),
                {n: None if np.isnan(v) else float(v) for n, v in zip(names, row, strict=True)},
            )
