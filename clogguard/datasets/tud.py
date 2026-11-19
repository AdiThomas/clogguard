"""TU Darmstadt hotend force dataset loader (TUdatalib 4596, DOI 10.48328/tudatalib-1772).

The file layout is not known before download, so the loader is flexible: it reads any CSV,
Parquet or (with h5py) HDF5 table, finds a time column and a force column, converts force to
grams (``force_g``) and keeps every other numeric column (print settings, G-code state) under a
sanitised name. Each table becomes one ``Run`` at the 10 Hz base rate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from clogguard.core.schema import Run, RunMetadata
from clogguard.datasets.registry import REGISTRY
from clogguard.datasets.tabular import (
    find_files,
    find_time_column,
    read_table,
    resample_to_base,
    sanitize,
    time_to_seconds,
)

NAME = "tud_force"
G_PER_N = 1000.0 / 9.80665

# Settings the dataset varies; columns whose names contain these words are reported in notes.
SETTING_WORDS = {
    "extrusion_multiplier": ("multiplier", "extrusion_mult", "flow"),
    "speed": ("speed", "feedrate", "feed"),
    "nozzle_temperature": ("temp", "temperature"),
    "line_width": ("line_width", "width"),
    "layer_height": ("layer_height", "layer", "height"),
}


def find_force_column(df: pd.DataFrame) -> tuple[str, float]:
    """Return (column, factor to grams)."""
    cands = []
    for c in df.columns:
        key = str(c).lower()
        # ASSUMPTION: the force column is named force/kraft/load/weight (German or English).
        if any(w in key for w in ("force", "kraft", "load", "weight", "gewicht")) and (
            pd.api.types.is_numeric_dtype(df[c])
        ):
            cands.append(c)
    if not cands:
        raise ValueError(f"no force column found in {list(df.columns)}")
    col = cands[0]
    key = str(col).lower()
    # ASSUMPTION: unit from the column name; no unit means grams.
    if re.search(r"(\bn\b|_n$|\[n\]|\(n\)|newton)", key):
        return col, G_PER_N
    if re.search(r"(\bkg\b|_kg$|\[kg\]|\(kg\))", key):
        return col, 1000.0
    if re.search(r"(\bmn\b|_mn$|\[mn\]|\(mn\))", key):
        return col, G_PER_N / 1000.0
    return col, 1.0


def table_to_frame(
    df: pd.DataFrame, rate_hz: float | None = None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Map a raw table to a base-rate frame. Returns (frame, info for notes)."""
    tcol = find_time_column(df)
    if tcol is None:
        if not rate_hz:
            raise ValueError(
                "no time column found; pass rate_hz (the acquisition sample rate) to convert"
            )
        t = np.arange(len(df)) / rate_hz
    else:
        t = time_to_seconds(df[tcol], nominal_rate_hz=rate_hz)
    fcol, factor = find_force_column(df)
    work = pd.DataFrame({"force_g": pd.to_numeric(df[fcol], errors="coerce") * factor})
    kept: list[str] = []
    for c in df.columns:
        if c in (tcol, fcol) or not pd.api.types.is_numeric_dtype(df[c]):
            continue
        name = sanitize(c)
        if name in work.columns or name == "t":
            name = f"{name}_2"
        work[name] = df[c].to_numpy()
        kept.append(name)
    frame = resample_to_base(work, t)
    settings = sorted(
        {s for s, words in SETTING_WORDS.items() for k in kept if any(w in k for w in words)}
    )
    info = {
        "time_column": str(tcol),
        "force_column": f"{fcol} (x{factor:.4g} to g)",
        "settings_present": ",".join(settings) or "none detected",
    }
    return frame, info


def load_file(path: str | Path, rate_hz: float | None = None) -> Run:
    p = Path(path)
    frame, info = table_to_frame(read_table(p), rate_hz=rate_hz)
    run_id = sanitize(f"tud_{p.stem}")[:80]
    notes = (
        f"source file {p.name}; time column {info['time_column']}; force column "
        f"{info['force_column']}; misconfigured settings present as columns: "
        f"{info['settings_present']}. Induction recorded as 'none': the settings are "
        "deliberate misconfigurations, not clogs."
    )
    meta = RunMetadata(
        run_id=run_id,
        data_source="open_dataset",
        # ASSUMPTION: no recording date is known from the files; file mtime is used.
        started_at=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
        printer="TU Darmstadt test printer",
        part=f"TUD force trial {p.stem}",
        source_citation=REGISTRY[NAME].citation,
        notes=notes,
    )
    return Run(meta=meta, frame=frame)


def load_dir(root: str | Path, rate_hz: float | None = None, limit: int | None = None) -> list[Run]:
    files = find_files(Path(root), ("**/*",))
    return [load_file(f, rate_hz=rate_hz) for f in files[:limit]]
