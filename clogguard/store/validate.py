"""Run validation: catch broken or leaky data before it reaches training or evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from clogguard.core.channels import ChannelConfig
from clogguard.core.schema import BASE_RATE_HZ, Run

# ASSUMPTION: without a channel config, columns with these prefixes are treated as label
# (ground-truth) channels. The simulator should name its truth columns accordingly.
LABEL_PREFIXES: tuple[str, ...] = ("true_", "label_")

GAP_FACTOR = 1.5  # a step larger than 1.5 base periods is a gap


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Literal["error", "warning"]
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


def _label_columns(run: Run, label_channels: Iterable[str] | None) -> list[str]:
    cols = [c for c in run.frame.columns if c != "t"]
    if label_channels is not None:
        wanted = set(label_channels)
        return [c for c in cols if c in wanted]
    return [c for c in cols if c.startswith(LABEL_PREFIXES)]


def validate_run(
    run: Run,
    *,
    run_dir: str | Path | None = None,
    channels: ChannelConfig | None = None,
    label_channels: Iterable[str] | None = None,
    base_rate_hz: float = BASE_RATE_HZ,
) -> list[Issue]:
    """Check one run. Returns a list of issues; empty means valid.

    Rules: ``t`` present and strictly increasing; no base-rate gaps; declared channels present;
    label-role channels only in simulated runs; defect ratings sane; open-dataset runs cite
    their source; manipulated variables exist as columns; listed photos exist (if
    ``run_dir`` is given).
    """
    issues: list[Issue] = []
    meta, frame = run.meta, run.frame

    # --- time axis ---------------------------------------------------------------------
    t_max: float | None = None
    if "t" not in frame.columns:
        issues.append(Issue("no_time_column", "error", "time series has no 't' column"))
    elif len(frame) > 0:
        t = frame["t"].to_numpy(dtype=float)
        if np.isnan(t).any():
            issues.append(Issue("t_nan", "error", f"{int(np.isnan(t).sum())} NaN timestamps"))
        dt = np.diff(t)
        bad = int((dt <= 0).sum())
        if bad:
            issues.append(
                Issue("t_not_monotonic", "error", f"t not strictly increasing at {bad} rows")
            )
        gap_limit = GAP_FACTOR / base_rate_hz
        gaps = dt[dt > gap_limit]
        if gaps.size:
            issues.append(
                Issue(
                    "base_rate_gap",
                    "warning",
                    f"{gaps.size} gaps longer than {gap_limit:.3f} s (longest {gaps.max():.2f} s)",
                )
            )
        t_max = float(np.nanmax(t))
    else:
        issues.append(Issue("empty_timeseries", "warning", "time series has no rows"))

    # --- channels ----------------------------------------------------------------------
    missing = [c for c in meta.channels if c not in frame.columns]
    if missing:
        issues.append(
            Issue("declared_channel_missing", "error", f"declared channels missing: {missing}")
        )
    if channels is not None and label_channels is None:
        label_channels = channels.names("label")
    labels = _label_columns(run, label_channels)
    if labels and meta.data_source != "simulated":
        issues.append(
            Issue(
                "label_channel_in_non_simulated",
                "error",
                f"label channels {labels} present in a {meta.data_source} run",
            )
        )
    absent_mv = [v for v in meta.manipulated_variables if v not in frame.columns]
    if absent_mv:
        issues.append(
            Issue(
                "manipulated_variable_missing",
                "error",
                f"manipulated variables not in time series: {absent_mv}",
            )
        )

    # --- provenance ----------------------------------------------------------------------
    if meta.data_source == "open_dataset" and not meta.source_citation.strip():
        issues.append(Issue("missing_citation", "error", "open_dataset run has no source_citation"))

    # --- defect ratings ------------------------------------------------------------------
    raters = [r.rater.strip() for r in meta.defect_ratings]
    if any(not r for r in raters):
        issues.append(Issue("rating_no_rater", "error", "defect rating without a rater name"))
    dupes = sorted({r for r in raters if r and raters.count(r) > 1})
    if dupes:
        issues.append(Issue("rating_duplicate_rater", "error", f"raters rated twice: {dupes}"))
    for r in meta.defect_ratings:
        if r.t_s is None:
            continue
        if r.t_s < 0 or (t_max is not None and r.t_s > t_max + 1e-6):
            issues.append(
                Issue(
                    "rating_out_of_range",
                    "error",
                    f"rater {r.rater!r} marked t={r.t_s} s outside the run (0..{t_max})",
                )
            )
        elif meta.photos and all(abs(p.t_s - r.t_s) > 1e-6 for p in meta.photos):
            issues.append(
                Issue(
                    "rating_not_on_photo",
                    "warning",
                    f"rater {r.rater!r} marked t={r.t_s} s, which is not a photo time",
                )
            )

    # --- files ---------------------------------------------------------------------------
    if run_dir is not None:
        lost = [p.path for p in meta.photos if not Path(run_dir, p.path).is_file()]
        if lost:
            issues.append(Issue("photo_missing", "error", f"photo files missing: {lost[:5]}"))
    return issues
