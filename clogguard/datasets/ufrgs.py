"""UFRGS vibration dataset loader (Zenodo 22716165, CC BY 4.0).

File names carry the print number(s), the condition label and a timestamp, e.g.
``benchy_1_healthy_08_01_13_38_00.parquet`` or ``benchy_11_12_temp_230_08_02_16_48_42.parquet``
(two prints in one file). Each file is converted to one or two core ``Run`` objects at 10 Hz with
``accel_rms`` and four band energies computed from the raw 3200 Hz stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from clogguard.core.schema import InductionSpec, Run, RunMetadata
from clogguard.datasets.accel import accel_tick_features
from clogguard.datasets.registry import REGISTRY, UFRGS_LABELS
from clogguard.datasets.tabular import find_time_column, time_to_seconds

NAME = "ufrgs_vibration"
NOMINAL_FS_HZ = 3200.0
PRINTER = "Creality Ender 3 V2 (UFRGS)"
# ASSUMPTION: the file names omit the year; the recordings are taken as made in 2025.
ASSUMED_YEAR = 2025
# ASSUMPTION: two prints in one file are separated by the largest time gap if it exceeds this.
SPLIT_GAP_S = 5.0

_NAME_RE = re.compile(
    r"^benchy_(?P<ids>\d+(?:_\d+)*)_(?P<label>"
    + "|".join(sorted(UFRGS_LABELS, key=len, reverse=True))
    + r")_(?P<mo>\d{2})_(?P<day>\d{2})_(?P<h>\d{2})_(?P<mi>\d{2})_(?P<s>\d{2})$"
)

NOZZLE_SEVERITY = {"nozzle_03": ("0.3mm", 0.3), "nozzle_02": ("0.2mm", 0.2)}


@dataclass(frozen=True)
class UfrgsFileInfo:
    path_stem: str
    print_ids: tuple[int, ...]
    label: str
    started_at: datetime


def parse_filename(name: str | Path) -> UfrgsFileInfo:
    """Parse a UFRGS file name (with or without directory and extension)."""
    stem = Path(name).name
    for suffix in (".parquet", ".csv"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    m = _NAME_RE.match(stem)
    if not m:
        raise ValueError(f"not a UFRGS file name: {name}")
    started = datetime(
        ASSUMED_YEAR, int(m["mo"]), int(m["day"]), int(m["h"]), int(m["mi"]), int(m["s"])
    )
    ids = tuple(int(i) for i in m["ids"].split("_"))
    return UfrgsFileInfo(stem, ids, m["label"], started)


def detect_columns(df: pd.DataFrame) -> tuple[str | None, tuple[str, str, str]]:
    """Return (time column or None, (x, y, z) columns)."""
    tcol = find_time_column(df)
    numeric = [c for c in df.columns if c != tcol and pd.api.types.is_numeric_dtype(df[c])]
    lower = {str(c).lower(): c for c in numeric}
    for pattern in (
        "{a}",
        "acc_{a}",
        "accel_{a}",
        "acceleration_{a}",
        "a{a}",
        "{a}_acc",
        "{a}_accel",
        "{a}_g",
    ):
        names = [pattern.format(a=a) for a in "xyz"]
        if all(n in lower for n in names):
            return tcol, (lower[names[0]], lower[names[1]], lower[names[2]])
    # ASSUMPTION: otherwise, columns ending in x, y, z (e.g. "AccX") are the axes.
    ends: dict[str, str] = {}
    for key, c in sorted(lower.items(), key=lambda kv: "acc" not in kv[0]):  # "acc*" first
        tail = re.sub(r"[\(\[].*?[\)\]]", "", key).strip(" _")  # drop units like "(g)"
        if tail and tail[-1] in "xyz" and tail[-1] not in ends:
            ends[tail[-1]] = c
    if len(ends) == 3:
        return tcol, (ends["x"], ends["y"], ends["z"])
    # ASSUMPTION: last resort, the first three numeric non-time columns are x, y, z.
    if len(numeric) >= 3:
        return tcol, (numeric[0], numeric[1], numeric[2])
    raise ValueError(f"cannot find three accelerometer columns in {list(df.columns)}")


def _split_two_prints(t: np.ndarray) -> list[slice]:
    order_ok = np.all(np.diff(t) >= 0)
    if not order_ok or t.size < 2:
        return [slice(0, t.size)]
    gaps = np.diff(t)
    i = int(np.argmax(gaps))
    if gaps[i] < SPLIT_GAP_S:
        return [slice(0, t.size)]
    return [slice(0, i + 1), slice(i + 1, t.size)]


def _meta(info: UfrgsFileInfo, ids: tuple[int, ...], extra_note: str = "") -> RunMetadata:
    label = info.label
    ids_s = "_".join(str(i) for i in ids)
    if label in NOZZLE_SEVERITY:
        severity, nozzle = NOZZLE_SEVERITY[label]
        induction = InductionSpec(method="small_nozzle", severity=severity)
    else:
        nozzle = 0.4  # ASSUMPTION: stock 0.4 mm nozzle for every non-restricted print
        induction = InductionSpec(method="none")
    notes = f"fault={label} ({UFRGS_LABELS[label]}); source file {info.path_stem}"
    if extra_note:
        notes += "; " + extra_note
    return RunMetadata(
        run_id=f"ufrgs_benchy_{ids_s}_{label}",
        data_source="open_dataset",
        started_at=info.started_at,
        purpose="other",
        printer=PRINTER,
        part=f"3DBenchy; fault={label}",
        material="PLA",  # ASSUMPTION: PLA, as is usual for Benchy prints on an Ender 3 V2
        nozzle_mm=nozzle,
        induction=induction,
        source_citation=REGISTRY[NAME].citation,
        notes=notes,
    )


def load_file(path: str | Path, nominal_fs_hz: float = NOMINAL_FS_HZ) -> list[Run]:
    """Convert one UFRGS Parquet (or CSV) file to Run objects at the base rate."""
    p = Path(path)
    info = parse_filename(p)
    df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
    tcol, axes = detect_columns(df)
    if tcol is None:
        # ASSUMPTION: without a time column, samples are uniform at the nominal rate.
        t = np.arange(len(df)) / nominal_fs_hz
        fs: float | None = nominal_fs_hz
    else:
        t = time_to_seconds(df[tcol], nominal_rate_hz=nominal_fs_hz)
        fs = None
    xyz = df[list(axes)].to_numpy(dtype=float)
    segments = _split_two_prints(t) if len(info.print_ids) == 2 else [slice(0, len(t))]
    runs: list[Run] = []
    for k, seg in enumerate(segments):
        if len(segments) == 2:
            ids: tuple[int, ...] = (info.print_ids[k],)
            note = f"print {k + 1} of 2 in this file, split at the largest time gap"
        else:
            ids = info.print_ids
            note = "two prints in one file, not split (no clear gap)" if len(ids) > 1 else ""
        frame = accel_tick_features(t[seg], xyz[seg], fs=fs)
        runs.append(Run(meta=_meta(info, ids, note), frame=frame))
    return runs


def fault_label(meta: RunMetadata) -> str:
    """The UFRGS condition label of a converted run (from ``part``)."""
    m = re.search(r"fault=([a-z0-9_]+)", meta.part)
    if not m:
        raise ValueError(f"{meta.run_id} has no UFRGS fault label")
    return m.group(1)


def iter_files(root: str | Path) -> list[Path]:
    root = Path(root)
    files = sorted(root.glob("benchy_*.parquet")) + sorted(root.glob("benchy_*.csv"))
    return [f for f in files if _NAME_RE.match(f.stem)]


def load_dir(root: str | Path, limit: int | None = None) -> list[Run]:
    runs: list[Run] = []
    for f in iter_files(root)[:limit]:
        runs.extend(load_file(f))
    return runs
