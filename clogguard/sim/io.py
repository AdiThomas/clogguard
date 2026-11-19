"""Tiny run writer/reader for the core run-directory layout (meta.json + timeseries.parquet).

Kept local to the simulator so it does not depend on the run store being built in parallel;
the layout is the one documented in ``clogguard/core/schema.py``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from clogguard.core.schema import META_FILE, TIMESERIES_FILE, Run, RunMetadata


def write_run(run: Run, root: str | Path, overwrite: bool = False) -> Path:
    """Write ``run`` to ``root/<run_id>/``. Returns the run directory."""
    out = Path(root) / run.run_id
    if out.exists() and not overwrite and any(out.iterdir()):
        raise FileExistsError(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    frame = run.frame.sort_values("t").reset_index(drop=True)
    frame.to_parquet(out / TIMESERIES_FILE, index=False)
    (out / META_FILE).write_text(run.meta.model_dump_json(indent=2), encoding="utf-8")
    return out


def read_run(path: str | Path) -> Run:
    """Read a run directory written by ``write_run`` (or any core-layout writer)."""
    p = Path(path)
    meta = RunMetadata.model_validate_json((p / META_FILE).read_text(encoding="utf-8"))
    frame = pd.read_parquet(p / TIMESERIES_FILE)
    return Run(meta=meta, frame=frame)
