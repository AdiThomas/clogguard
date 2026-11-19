"""CAXTON label-CSV loader (Brion & Pattinson, 2022; Cambridge Apollo repository).

Only the label CSV is read; the 1,272,273 images are never loaded. Columns are normalised to:
``img_path, timestamp, flow_rate, feed_rate (lateral speed), z_offset, target_hotend, hotend,
bed, nozzle_tip_x, nozzle_tip_y`` plus ``flow_rate_class`` (low / good / high).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

NAME = "caxton"

# normalised name -> accepted source names (lower case)
ALIASES: dict[str, tuple[str, ...]] = {
    "img_path": ("img_path", "image_path", "image", "path", "filename"),
    "timestamp": ("timestamp", "time", "datetime"),
    "flow_rate": ("flow_rate", "flowrate", "flow"),
    "feed_rate": ("feed_rate", "feedrate", "lateral_speed", "speed"),
    "z_offset": ("z_offset", "zoffset", "z_off"),
    "target_hotend": ("target_hotend", "hotend_target", "target"),
    "hotend": ("hotend", "hotend_temp", "hotend_temperature", "nozzle_temp"),
    "bed": ("bed", "bed_temp", "bed_temperature"),
    "nozzle_tip_x": ("nozzle_tip_x", "tip_x", "nozzle_x"),
    "nozzle_tip_y": ("nozzle_tip_y", "tip_y", "nozzle_y"),
    "flow_rate_class": ("flow_rate_class", "flow_class"),
}

FLOW_CLASSES = ("low", "good", "high")
# ASSUMPTION: numeric class codes in the CSV are 0 = low, 1 = good, 2 = high.
CLASS_CODES = {0: "low", 1: "good", 2: "high"}
# ASSUMPTION: when no class column exists, flow rate (in %) is binned: < 90 low, > 110 high.
FLOW_GOOD_RANGE = (90.0, 110.0)


def _rename_map(columns: Iterable[str]) -> dict[str, str]:
    lower = {str(c).strip().lower(): c for c in columns}
    out: dict[str, str] = {}
    for norm, names in ALIASES.items():
        for n in names:
            if n in lower and lower[n] not in out:
                out[lower[n]] = norm
                break
    return out


def flow_class(flow_rate: pd.Series) -> pd.Series:
    lo, hi = FLOW_GOOD_RANGE
    v = pd.to_numeric(flow_rate, errors="coerce").to_numpy()
    cls = np.where(v < lo, "low", np.where(v > hi, "high", "good"))
    return pd.Series(cls, index=flow_rate.index).where(~np.isnan(v))


def _normalise(chunk: pd.DataFrame) -> pd.DataFrame:
    chunk = chunk.rename(columns=_rename_map(chunk.columns))
    keep = [c for c in ALIASES if c in chunk.columns]
    chunk = chunk[keep].copy()
    if "flow_rate_class" in chunk.columns:
        col = chunk["flow_rate_class"]
        if pd.api.types.is_numeric_dtype(col):
            chunk["flow_rate_class"] = col.map(CLASS_CODES)
        else:
            chunk["flow_rate_class"] = col.astype(str).str.strip().str.lower()
    elif "flow_rate" in chunk.columns:
        chunk["flow_rate_class"] = flow_class(chunk["flow_rate"])
    return chunk


def load_labels(
    path: str | Path,
    flow_classes: Iterable[str] | None = None,
    max_rows: int | None = None,
    chunksize: int = 200_000,
) -> pd.DataFrame:
    """Read the label CSV in chunks, optionally keeping only some flow-rate classes."""
    wanted = None if flow_classes is None else {c.lower() for c in flow_classes}
    if wanted is not None and not wanted <= set(FLOW_CLASSES):
        raise ValueError(f"flow classes must be among {FLOW_CLASSES}")
    parts: list[pd.DataFrame] = []
    n = 0
    for chunk in pd.read_csv(path, chunksize=chunksize):
        c = _normalise(chunk)
        if wanted is not None:
            c = c[c["flow_rate_class"].isin(wanted)]
        parts.append(c)
        n += len(c)
        if max_rows is not None and n >= max_rows:
            break
    if not parts:
        return pd.DataFrame(columns=list(ALIASES))
    out = pd.concat(parts, ignore_index=True)
    return out.head(max_rows) if max_rows is not None else out


def find_label_csv(root: str | Path) -> Path:
    csvs = sorted(Path(root).glob("*.csv"))
    if not csvs:
        raise FileNotFoundError(f"no label CSV in {root}")
    return max(csvs, key=lambda p: p.stat().st_size)
