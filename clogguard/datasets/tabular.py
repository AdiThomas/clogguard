"""Format-agnostic helpers: read a table, find the time column, resample to the base rate.

The open datasets do not document their column names, so the loaders detect them heuristically.
Every heuristic here is marked ``# ASSUMPTION:``.
"""

from __future__ import annotations

import csv
import importlib.util
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from clogguard.core.schema import BASE_RATE_HZ

TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".parquet", ".pq", ".h5", ".hdf5")

_TIME_NAMES = ("t", "time", "timestamp", "time_s", "t_s", "ts", "seconds", "sec", "elapsed")


def sanitize(name: str) -> str:
    """Column name -> channel-style name (``[a-z][a-z0-9_]*``, at most 63 chars)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "c_" + s
    return s[:63].rstrip("_")


def read_table(path: str | Path) -> pd.DataFrame:
    """Read CSV/TSV/TXT, Parquet, or HDF5 (only if h5py is importable)."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(p)
    if suffix in (".h5", ".hdf5"):
        return _read_hdf5(p)
    if suffix in (".csv", ".tsv", ".txt"):
        head = p.read_text(encoding="utf-8", errors="replace")[:8192]
        try:
            sep = csv.Sniffer().sniff(head, delimiters=",;\t ").delimiter
        except csv.Error:
            sep = "\t" if suffix == ".tsv" else ","
        # ASSUMPTION: a comma decimal separator only appears with ';' delimiters (European CSV).
        decimal = "," if sep == ";" and re.search(r"\d,\d", head) else "."
        return pd.read_csv(p, sep=sep, decimal=decimal, skipinitialspace=True)
    raise ValueError(f"unsupported table format {suffix!r} for {p.name}")


def _read_hdf5(p: Path) -> pd.DataFrame:
    if importlib.util.find_spec("h5py") is None:
        raise RuntimeError(
            f"{p.name} is HDF5 but h5py is not installed. Install it (pip install h5py) or "
            "convert the file to CSV/Parquet first."
        )
    import h5py  # type: ignore[import-not-found]  # pragma: no cover

    cols: dict[str, np.ndarray] = {}  # pragma: no cover

    def visit(name: str, obj: object) -> None:  # pragma: no cover
        if isinstance(obj, h5py.Dataset) and obj.ndim == 1:
            cols[name.replace("/", "_")] = obj[()]

    with h5py.File(p, "r") as f:  # pragma: no cover
        f.visititems(visit)
    n = min((len(v) for v in cols.values()), default=0)  # pragma: no cover
    return pd.DataFrame({k: v[:n] for k, v in cols.items()})  # pragma: no cover


def find_time_column(df: pd.DataFrame) -> str | None:
    """Best guess at the time column, or None."""
    lower = {str(c).lower().strip(): c for c in df.columns}
    for cand in _TIME_NAMES:
        if cand in lower:
            return lower[cand]
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            return c
    # ASSUMPTION: any column whose name contains "time" or "stamp" is a time axis.
    for key, c in lower.items():
        if ("time" in key or "stamp" in key) and pd.api.types.is_numeric_dtype(df[c]):
            return c
    return None


def time_to_seconds(series: pd.Series, nominal_rate_hz: float | None = None) -> np.ndarray:
    """Convert a time column to seconds from its first sample.

    Datetimes are converted exactly. Numeric columns have unknown units; the scale (s, ms, us,
    ns) is chosen so the implied sample rate is plausible.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return (series - series.iloc[0]).dt.total_seconds().to_numpy(dtype=float)
    v = series.to_numpy(dtype=float)
    v = v - v[0]
    diffs = np.diff(v)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return v
    dt = float(np.median(diffs))
    # ASSUMPTION: the true sample rate is near nominal_rate_hz if given, else in 0.5 Hz..50 kHz;
    # pick the unit whose implied rate is closest (log scale) to nominal or inside that range.
    scales = (1.0, 1e-3, 1e-6, 1e-9)
    if nominal_rate_hz:
        best = min(scales, key=lambda s: abs(np.log10((1.0 / (dt * s)) / nominal_rate_hz)))
    else:
        best = next((s for s in scales if 0.5 <= 1.0 / (dt * s) <= 50_000), 1.0)
    return v * best


def tick_index(t_s: np.ndarray, rate_hz: float = BASE_RATE_HZ) -> np.ndarray:
    """Tick number of each sample; tick k covers [k/rate, (k+1)/rate)."""
    return np.floor(np.asarray(t_s, dtype=float) * rate_hz + 1e-9).astype(np.int64)


def tick_times(ticks: np.ndarray, rate_hz: float = BASE_RATE_HZ) -> np.ndarray:
    """Time stamp of a tick: the END of its window, so a row only summarises past samples."""
    return np.round((np.asarray(ticks) + 1) / rate_hz, 6)


def resample_to_base(
    df: pd.DataFrame,
    t_s: np.ndarray,
    rate_hz: float = BASE_RATE_HZ,
    agg: str | Callable[..., object] = "mean",
) -> pd.DataFrame:
    """Aggregate numeric columns of ``df`` into base-rate ticks. Returns ``t`` + columns.

    Ticks with no samples are dropped (not interpolated); gaps stay visible.
    """
    num = df.select_dtypes(include=[np.number, "bool"]).astype(float)
    num = num.drop(columns=[c for c in num.columns if c == "t"])
    ticks = tick_index(t_s, rate_hz)
    grouped = num.groupby(ticks, sort=True).agg(agg)
    out = grouped.reset_index(drop=True)
    out.insert(0, "t", tick_times(grouped.index.to_numpy(), rate_hz))
    return out


def find_files(root: Path, patterns: tuple[str, ...] = ("*",)) -> list[Path]:
    files: set[Path] = set()
    for pat in patterns:
        files.update(p for p in root.glob(pat) if p.is_file())
    return sorted(f for f in files if f.suffix.lower() in TABLE_SUFFIXES)
