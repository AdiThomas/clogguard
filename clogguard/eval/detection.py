"""Offline detector evaluation: alert times from per-run score frames, lead times and false
alerts, and a threshold sweep (lead time vs false alarms). Pure pandas.

Scores: one DataFrame per run with columns ``t`` (s) and ``p_clog`` (0..1), causal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from clogguard.core.schema import RunMetadata
from clogguard.eval.stats import clopper_pearson_upper

LEAD_TARGET_S = 300.0


def alert_time(scores: pd.DataFrame, threshold: float, persist_s: float = 0.0) -> float | None:
    """First time p_clog has been >= threshold continuously for ``persist_s`` seconds.

    The alert is raised at the end of the persistence window (causal). NaN counts as below.
    """
    if scores.empty:
        return None
    df = scores.sort_values("t")
    t = df["t"].to_numpy(float)
    above = (df["p_clog"].to_numpy(float) >= threshold) & ~np.isnan(df["p_clog"].to_numpy(float))
    start: float | None = None
    for ti, a in zip(t, above, strict=True):
        if not a:
            start = None
            continue
        if start is None:
            start = ti
        if ti - start >= persist_s:
            return float(ti)
    return None


@dataclass
class DetectionResult:
    threshold: float
    persist_s: float
    # columns: run_id, is_clog, data_source, alert_t_s, onset_s, lead_s, miss, false_alert
    per_run: pd.DataFrame
    data_source: str

    @property
    def clog(self) -> pd.DataFrame:
        return self.per_run[self.per_run["is_clog"]]

    @property
    def clean(self) -> pd.DataFrame:
        return self.per_run[~self.per_run["is_clog"]]

    def summary(self) -> dict[str, float | int | str | None]:
        clog = self.clog
        rated = clog[clog["lead_s"].notna()]
        clean = self.clean
        n_fa = int(clean["false_alert"].sum())
        n_clean = len(clean)
        leads = rated["lead_s"].to_numpy(float)
        return {
            "threshold": self.threshold,
            "persist_s": self.persist_s,
            "data_source": self.data_source,
            "n_clog": len(rated),
            "n_clean": n_clean,
            "median_lead_min": float(np.median(leads)) / 60.0 if len(leads) else None,
            "frac_lead_ge_5min": float((leads >= LEAD_TARGET_S).mean()) if len(leads) else None,
            "missed": int(rated["miss"].sum()),
            "false_alerts": n_fa,
            "false_alert_rate": n_fa / n_clean if n_clean else None,
            "false_alert_upper95": clopper_pearson_upper(n_fa, n_clean) if n_clean else None,
        }


def evaluate_alerts(
    scores: Mapping[str, pd.DataFrame],
    metas: Mapping[str, RunMetadata],
    threshold: float,
    persist_s: float = 0.0,
) -> DetectionResult:
    """Alert time per run, lead time on clog runs (onset - alert; miss = 0 if no alert, negative
    if late; NaN if the run has no rated onset), false alert on clean runs (any alert)."""
    missing = sorted(set(scores) - set(metas))
    if missing:
        raise KeyError(f"no metadata for runs: {missing}")
    rows = []
    for run_id in sorted(scores):
        meta = metas[run_id]
        at = alert_time(scores[run_id], threshold, persist_s)
        is_clog = meta.is_clog_run
        onset = meta.defect_onset_s
        lead: float | None = None
        miss = False
        if is_clog and onset is not None:
            if at is None:
                lead, miss = 0.0, True
            else:
                lead = onset - at
                miss = at >= onset
        rows.append(
            {
                "run_id": run_id,
                "is_clog": is_clog,
                "data_source": meta.data_source,
                "alert_t_s": at,
                "onset_s": onset,
                "lead_s": lead,
                "miss": miss,
                "false_alert": (not is_clog) and at is not None,
            }
        )
    per_run = pd.DataFrame(
        rows,
        columns=[
            "run_id", "is_clog", "data_source", "alert_t_s", "onset_s", "lead_s", "miss",
            "false_alert",
        ],
    )
    per_run["lead_s"] = per_run["lead_s"].astype(float)
    src = ",".join(sorted({m.data_source for k, m in metas.items() if k in scores}))
    return DetectionResult(threshold, persist_s, per_run, src)


def sweep(
    scores: Mapping[str, pd.DataFrame],
    metas: Mapping[str, RunMetadata],
    thresholds: Iterable[float] = tuple(np.round(np.arange(0.05, 1.0, 0.05), 2)),
    persist_s: float | Iterable[float] = 0.0,
) -> pd.DataFrame:
    """Lead-time vs false-alarm table, one row per (threshold, persist_s)."""
    persists = [persist_s] if isinstance(persist_s, int | float) else list(persist_s)
    rows = [
        evaluate_alerts(scores, metas, float(th), float(p)).summary()
        for p in persists
        for th in thresholds
    ]
    return pd.DataFrame(rows)
