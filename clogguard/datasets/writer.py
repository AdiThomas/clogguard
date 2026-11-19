"""Tiny run writer/reader for converted open-dataset runs, in the core run layout.

Writes ``<root>/<run_id>/meta.json`` and ``timeseries.parquet``. The full run store
(``clogguard.store``) is built in parallel; this writer only uses the layout from
``clogguard.core.schema`` so the store can read what it writes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from clogguard.core.schema import META_FILE, TIMESERIES_FILE, Run, RunMetadata


def write_run(run: Run, root: str | Path, overwrite: bool = True) -> Path:
    run_dir = Path(root) / run.run_id
    if run_dir.exists() and not overwrite:
        raise FileExistsError(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    frame = run.frame.sort_values("t").reset_index(drop=True)
    meta = run.meta.model_copy(update={"channels": [c for c in frame.columns if c != "t"]})
    frame.to_parquet(run_dir / TIMESERIES_FILE, index=False)
    (run_dir / META_FILE).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    return run_dir


def read_run(run_dir: str | Path) -> Run:
    d = Path(run_dir)
    meta = RunMetadata.model_validate_json((d / META_FILE).read_text(encoding="utf-8"))
    return Run(meta=meta, frame=pd.read_parquet(d / TIMESERIES_FILE))
