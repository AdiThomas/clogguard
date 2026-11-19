"""AGH 3D-printing anomaly dataset loader (github.com/joanna-/3D-Printing-Data, CC BY 4.0).

CSV files with a head accelerometer, power and temperatures recorded while malfunctions were
provoked. Column names are detected:

* time -> ``t`` (seconds from the first sample)
* accelerometer x/y/z -> per-tick ``accel_rms`` (mean removed per tick) and per-axis means
  ``accel_x``, ``accel_y``, ``accel_z``; band energies only when the rate allows (>= 400 Hz)
* power -> ``power_w``; temperatures -> ``temp_<name>_c``
* a label / anomaly / malfunction column -> ``anomaly_label`` (ground truth; never a model input)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from clogguard.core.schema import Run, RunMetadata
from clogguard.datasets.accel import accel_tick_features
from clogguard.datasets.registry import REGISTRY
from clogguard.datasets.tabular import (
    find_files,
    find_time_column,
    read_table,
    resample_to_base,
    sanitize,
    time_to_seconds,
)

NAME = "agh_anomaly"
LABEL_WORDS = ("label", "anomaly", "malfunction", "fault", "class")
MIN_RATE_FOR_BANDS_HZ = 400.0


def classify_columns(df: pd.DataFrame) -> dict[str, object]:
    tcol = find_time_column(df)
    accel: dict[str, str] = {}
    power: str | None = None
    temps: list[str] = []
    label: str | None = None
    for c in df.columns:
        if c == tcol:
            continue
        key = str(c).lower().strip()
        bare = re.sub(r"[\(\[].*?[\)\]]", "", key).strip(" _")
        if any(w in key for w in LABEL_WORDS):
            label = label or c
            continue
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        # ASSUMPTION: accelerometer axes are "x/y/z", "acc_x", "accX", "ax" ...
        m = re.fullmatch(r"(?:acc(?:el(?:eration)?)?[ _]?|a)?([xyz])(?:[ _]?acc(?:el)?)?", bare)
        if m and m.group(1) not in accel:
            accel[m.group(1)] = c
        elif "power" in key or "watt" in key:
            power = power or c
        elif "temp" in key:
            temps.append(c)
    return {
        "time": tcol,
        "accel": accel if len(accel) == 3 else {},
        "power": power,
        "temps": temps,
        "label": label,
    }


def table_to_frame(df: pd.DataFrame, rate_hz: float | None = None) -> pd.DataFrame:
    cols = classify_columns(df)
    tcol = cols["time"]
    if tcol is None:
        if not rate_hz:
            raise ValueError("no time column found; pass rate_hz to convert")
        t = np.arange(len(df)) / rate_hz
    else:
        t = time_to_seconds(df[tcol], nominal_rate_hz=rate_hz)
    work = pd.DataFrame(index=df.index)
    accel: dict[str, str] = cols["accel"]  # type: ignore[assignment]
    for axis, c in accel.items():
        work[f"accel_{axis}"] = pd.to_numeric(df[c], errors="coerce")
    if cols["power"] is not None:
        # ASSUMPTION: the power column is in watts.
        work["power_w"] = pd.to_numeric(df[cols["power"]], errors="coerce")
    for c in cols["temps"]:  # type: ignore[union-attr]
        name = sanitize(str(c)).removesuffix("_c")
        name = name if name.startswith("temp") else f"temp_{name}"
        work[f"{name}_c"] = pd.to_numeric(df[c], errors="coerce")
    if cols["label"] is not None:
        lab = df[cols["label"]]
        if not pd.api.types.is_numeric_dtype(lab):
            # ASSUMPTION: text labels; "normal"/"ok"/"none"/"" means no anomaly.
            norm = lab.astype(str).str.strip().str.lower()
            lab = (~norm.isin(["normal", "ok", "none", "", "nan", "0", "false"])).astype(float)
        work["anomaly_label"] = pd.to_numeric(lab, errors="coerce")
    agg = {c: ("max" if c == "anomaly_label" else "mean") for c in work.columns}
    frame = resample_to_base(work, t, agg=agg)  # type: ignore[arg-type]
    if accel:
        xyz = work[["accel_x", "accel_y", "accel_z"]].to_numpy(dtype=float)
        feats = accel_tick_features(t, xyz)
        d = np.diff(np.sort(t))
        d = d[d > 0]
        fs = 1.0 / float(np.median(d)) if d.size else 0.0
        if fs < MIN_RATE_FOR_BANDS_HZ:
            feats = feats[["t", "accel_rms"]]
        frame = frame.merge(feats, on="t", how="left")
    return frame


def load_file(path: str | Path, rate_hz: float | None = None) -> Run:
    p = Path(path)
    frame = table_to_frame(read_table(p), rate_hz=rate_hz)
    # ASSUMPTION: the provoked malfunction is named by the file or its folder.
    condition = f"{p.parent.name}/{p.stem}"
    meta = RunMetadata(
        run_id=sanitize(f"agh_{p.parent.name}_{p.stem}")[:80],
        data_source="open_dataset",
        # ASSUMPTION: recording dates are not in the files; file mtime is used.
        started_at=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
        printer="AGH test printer",
        part=f"AGH trial; condition={condition}",
        source_citation=REGISTRY[NAME].citation,
        notes=(
            f"source file {condition}{p.suffix}; induction recorded as 'none' (provoked "
            "malfunction, see condition). anomaly_label, if present, is ground truth only."
        ),
    )
    return Run(meta=meta, frame=frame)


def load_dir(root: str | Path, rate_hz: float | None = None, limit: int | None = None) -> list[Run]:
    files = [f for f in find_files(Path(root), ("**/*.csv",))]
    return [load_file(f, rate_hz=rate_hz) for f in files[:limit]]
