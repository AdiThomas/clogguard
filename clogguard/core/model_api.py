"""The interface between models (workstream D) and the real-time service (workstream E).

Offline: ``HealthModel.fit`` on stored runs, ``score_run`` on a whole run (causally: the value
at time t uses only rows with time <= t).
Online: ``make_online`` returns a fresh stateful scorer per print; the service feeds it new
base-rate rows and gets one Assessment per call.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from clogguard.core.events import Assessment
from clogguard.core.schema import Run

# Columns returned by HealthModel.score_run, aligned with the input frame's "t".
SCORE_COLUMNS = (
    "t",
    "health",
    "p_clog",
    "severity",
    "minutes_to_defect",
    "minutes_to_defect_lo",
    "minutes_to_defect_hi",
    "recoverable",
)


@runtime_checkable
class OnlineScorer(Protocol):
    def update(self, rows: pd.DataFrame, now_s: float) -> Assessment:
        """Consume new base-rate rows (may be empty) and assess the nozzle at ``now_s``.
        Must finish in under 100 ms on the Klipper host. Must not raise on missing columns:
        return state='unknown' with data_fresh=False instead."""
        ...


@runtime_checkable
class HealthModel(Protocol):
    model_id: str
    input_channels: list[str]

    def fit(self, runs: Sequence[Run]) -> HealthModel:
        """Train. Implementations must call ``run.model_inputs`` so manipulated variables and
        label channels are never inputs."""
        ...

    def score_run(self, run: Run) -> pd.DataFrame:
        """Causal scores for every row of ``run.frame``; columns SCORE_COLUMNS (NaN allowed)."""
        ...

    def make_online(self) -> OnlineScorer: ...

    def save(self, path: str | Path) -> None: ...
