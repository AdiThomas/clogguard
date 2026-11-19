"""One function per Table 2 criterion (docs/plan.md, Criteria and Test plan).

Each returns a :class:`CriterionResult`. ``supported_95`` is ``None`` where the plan states no
one-sided 95% rule for that criterion.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from clogguard.eval.runs import LoadedRun
from clogguard.eval.stats import (
    bootstrap_diff_ci,
    bootstrap_ratio_reduction_ci,
    clopper_pearson_lower,
    clopper_pearson_upper,
    mann_whitney,
    wilson_interval,
)

LEAD_TARGET_S = 300.0
LEAD_SUPPORT_FRACTION = 0.90  # proposed rule in docs/plan.md
RECOVERY_TARGET = 0.70
FALSE_ALARM_LIMIT = 0.10
WASTE_REDUCTION_TARGET = 0.50
MASS_TOLERANCE = 0.05
ASSESS_INTERVAL_S = 1.0
ACK_LIMIT_S = 2.0
COST_LIMIT_CAD = 400.0
INSTALL_LIMIT_MIN = 180.0


@dataclass
class CriterionResult:
    criterion: str
    value: float | None
    target: str
    meets_target: bool | None
    supported_95: bool | None
    n: int
    notes: list[str] = field(default_factory=list)
    data_source: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sources_of(runs: Sequence[LoadedRun]) -> str:
    """Comma-joined sorted set of data sources ("" for no runs)."""
    return ",".join(sorted({r.data_source for r in runs}))


# ---------------------------------------------------------------- lead time


def lead_time(runs: Sequence[LoadedRun]) -> CriterionResult:
    """Warning lead time on watch-only clog runs: defect onset minus alert time.

    A clog run with no alert before the defect is a miss: lead 0 if no alert, negative if late.
    Runs where the service acted are excluded (lead time is measured only without intervention).
    """
    clog = [r for r in runs if r.meta.is_clog_run]
    leads: dict[str, float] = {}
    excluded: dict[str, str] = {}
    misses: list[str] = []
    for r in clog:
        if r.meta.service_mode != "watch":
            excluded[r.run_id] = (
                "service acted (mode act)" if r.meta.service_mode == "act" else "service off"
            )
            continue
        if r.accepted_printer_commands():
            excluded[r.run_id] = "printer command accepted in watch mode"
            continue
        onset = r.meta.defect_onset_s
        if onset is None:
            excluded[r.run_id] = "no defect onset rated"
            continue
        alert = r.alert_time_s()
        if alert is None:
            leads[r.run_id] = 0.0
            misses.append(r.run_id)
        else:
            leads[r.run_id] = onset - alert
            if alert >= onset:
                misses.append(r.run_id)
    n = len(leads)
    vals = np.array(list(leads.values()), float)
    ok = int((vals >= LEAD_TARGET_S).sum()) if n else 0
    median = float(np.median(vals)) if n else None
    lower = clopper_pearson_lower(ok, n) if n else None
    notes = [f"excluded {k}: {v}" for k, v in sorted(excluded.items())]
    if misses:
        notes.append(f"misses (no alert before defect): {', '.join(sorted(misses))}")
    # ASSUMPTION: "5 min or more in at least 90% of runs" at one-sided 95% means the exact lower
    # bound on the fraction of runs with lead >= 5 min is at least 0.90.
    return CriterionResult(
        criterion="Warning lead time",
        value=None if median is None else median / 60.0,
        target="median >= 5 min (watch-only); supported: lower 95% bound of "
        "P(lead >= 5 min) >= 0.90",
        meets_target=None if median is None else median >= LEAD_TARGET_S,
        supported_95=None if lower is None else lower >= LEAD_SUPPORT_FRACTION,
        n=n,
        notes=notes,
        data_source=sources_of(clog),
        details={
            "unit": "min",
            "lead_s": leads,
            "n_ge_5min": ok,
            "fraction_ge_5min": ok / n if n else None,
            "fraction_lower95": lower,
            "excluded": excluded,
            "misses": sorted(misses),
        },
    )


# ---------------------------------------------------------------- recovery


def recovery_rate(runs: Sequence[LoadedRun]) -> CriterionResult:
    """validation_clog, service acting, recoverable: success = no rater marked a defect."""
    cand = [
        r
        for r in runs
        if r.meta.purpose == "validation_clog"
        and r.meta.service_mode == "act"
        and r.meta.induction.recoverable is True
    ]
    notes: list[str] = []
    rated = []
    for r in cand:
        if not r.meta.defect_ratings:
            notes.append(f"excluded {r.run_id}: no defect ratings")
        else:
            rated.append(r)
    succ = [r.run_id for r in rated if r.meta.defect_onset_s is None]
    n, x = len(rated), len(succ)
    lower = clopper_pearson_lower(x, n) if n else None
    return CriterionResult(
        criterion="Recovery rate",
        value=x / n if n else None,
        target="rate >= 0.70 (21/30); supported: lower 95% bound >= 0.70 (26/30 or 18/20)",
        meets_target=(x / n >= RECOVERY_TARGET) if n else None,
        supported_95=None if lower is None else lower >= RECOVERY_TARGET,
        n=n,
        notes=notes,
        data_source=sources_of(cand),
        details={
            "successes": x,
            "successful_runs": sorted(succ),
            "lower95": lower,
            "wilson95": wilson_interval(x, n) if n else None,
        },
    )


# ---------------------------------------------------------------- false alarms


def false_alarms(runs: Sequence[LoadedRun]) -> CriterionResult:
    """validation_clean runs: any accepted printer command is an unnecessary intervention.

    Notify alone (or an alert time with no printer command) is a false *alert*, reported
    separately and not counted as an intervention.
    """
    clean = [r for r in runs if r.meta.purpose == "validation_clean"]
    with_int: dict[str, int] = {}
    false_alerts: list[str] = []
    for r in clean:
        cmds = r.accepted_printer_commands()
        if cmds:
            with_int[r.run_id] = len(cmds)
        elif r.accepted_notifies() or r.alert_time_s() is not None:
            false_alerts.append(r.run_id)
    n, x = len(clean), len(with_int)
    upper = clopper_pearson_upper(x, n) if n else None
    notes = []
    if false_alerts:
        notes.append(f"false alerts without intervention: {', '.join(sorted(false_alerts))}")
    # ASSUMPTION: the binomial count is runs with at least one intervention ("3 or fewer of 30");
    # the total number of interventions is reported alongside.
    return CriterionResult(
        criterion="False alarms",
        value=x / n if n else None,
        target="<= 1 per 10 normal prints (<= 3/30); supported: upper 95% bound < 0.10 "
        "(0/30 or <= 1/50)",
        meets_target=(x / n <= FALSE_ALARM_LIMIT) if n else None,
        supported_95=None if upper is None else upper < FALSE_ALARM_LIMIT,
        n=n,
        notes=notes,
        data_source=sources_of(clean),
        details={
            "runs_with_intervention": x,
            "interventions_total": int(sum(with_int.values())),
            "interventions_by_run": with_int,
            "false_alert_runs": sorted(false_alerts),
            "false_alerts": len(false_alerts),
            "upper95": upper,
            "wilson95": wilson_interval(x, n) if n else None,
        },
    )


# ---------------------------------------------------------------- waste


def filament_waste(runs: Sequence[LoadedRun], seed: int = 0) -> CriterionResult:
    """Mean wasted filament, validation_clog with the system acting vs clog_no_intervention."""
    sys_runs = [
        r
        for r in runs
        if r.meta.purpose == "validation_clog"
        and r.meta.service_mode == "act"
        and r.meta.wasted_filament_g is not None
    ]
    base_runs = [
        r
        for r in runs
        if r.meta.purpose == "clog_no_intervention" and r.meta.wasted_filament_g is not None
    ]
    s = [float(r.meta.wasted_filament_g) for r in sys_runs]  # type: ignore[arg-type]
    b = [float(r.meta.wasted_filament_g) for r in base_runs]  # type: ignore[arg-type]
    notes: list[str] = []
    if not s or not b:
        notes.append("need both system and no-intervention runs with wasted_filament_g")
        return CriterionResult(
            criterion="Filament waste",
            value=None,
            target="mean reduced >= 50% vs no-intervention; supported: rank test",
            meets_target=None,
            supported_95=None,
            n=len(s) + len(b),
            notes=notes,
            data_source=sources_of(sys_runs + base_runs),
            details={"n_system": len(s), "n_no_intervention": len(b)},
        )
    red, red_lo, red_hi = bootstrap_ratio_reduction_ci(s, b, seed=seed)
    diff, d_lo, d_hi = bootstrap_diff_ci(b, s, "mean", seed=seed)
    plain = mann_whitney(s, b, "less")
    # ASSUMPTION: the rank test supports "reduced by at least half" when 2 x system waste is
    # stochastically smaller than no-intervention waste (one-sided Mann-Whitney, p < 0.05).
    half = mann_whitney([2 * v for v in s], b, "less")
    return CriterionResult(
        criterion="Filament waste",
        value=red,
        target="mean reduced >= 50% vs no-intervention; supported: rank test (one-sided 95%)",
        meets_target=red >= WASTE_REDUCTION_TARGET,
        supported_95=bool(half.p_value < 0.05),
        n=len(s) + len(b),
        notes=notes,
        data_source=sources_of(sys_runs + base_runs),
        details={
            "unit": "fraction reduction of mean",
            "n_system": len(s),
            "n_no_intervention": len(b),
            "mean_system_g": float(np.mean(s)),
            "mean_no_intervention_g": float(np.mean(b)),
            "reduction_ci95": [red_lo, red_hi],
            "mean_diff_g": diff,
            "mean_diff_ci95_g": [d_lo, d_hi],
            "u_less_p": plain.p_value,
            "u_half_less_p": half.p_value,
        },
    )


# ---------------------------------------------------------------- part quality


def part_quality(runs: Sequence[LoadedRun]) -> CriterionResult:
    """Recovered parts (validation_clog, act, recoverable, no defect) within 5% of reference.

    Reference: ``meta.reference_mass_g``; else the median part mass of ``reference_part`` runs.
    """
    refs = [
        r.meta.part_mass_g
        for r in runs
        if r.meta.purpose == "reference_part" and r.meta.part_mass_g is not None
    ]
    fallback = float(np.median(refs)) if refs else None
    recovered = [
        r
        for r in runs
        if r.meta.purpose == "validation_clog"
        and r.meta.service_mode == "act"
        and r.meta.induction.recoverable is True
        and r.meta.defect_ratings
        and r.meta.defect_onset_s is None
    ]
    notes: list[str] = []
    devs: dict[str, float] = {}
    for r in recovered:
        ref = r.meta.reference_mass_g if r.meta.reference_mass_g is not None else fallback
        if ref is None or r.meta.part_mass_g is None or ref <= 0:
            notes.append(f"excluded {r.run_id}: missing part or reference mass")
            continue
        devs[r.run_id] = (r.meta.part_mass_g - ref) / ref
    n = len(devs)
    within = sorted(k for k, v in devs.items() if abs(v) <= MASS_TOLERANCE)
    # ASSUMPTION: the target is met when every recovered part is within 5%.
    return CriterionResult(
        criterion="Part quality",
        value=len(within) / n if n else None,
        target="every recovered part within 5% of reference mass",
        meets_target=(len(within) == n) if n else None,
        supported_95=None,
        n=n,
        notes=notes,
        data_source=sources_of(recovered),
        details={
            "unit": "fraction within 5%",
            "deviation_by_run": devs,
            "max_abs_deviation": max((abs(v) for v in devs.values()), default=None),
            "reference_fallback_g": fallback,
        },
    )


# ---------------------------------------------------------------- response time


def response_time(runs: Sequence[LoadedRun]) -> CriterionResult:
    """Assessment interval and decision->ack latency, from audit logs."""
    intervals: list[float] = []
    latencies: list[float] = []
    with_audit = [r for r in runs if r.audit]
    for r in with_audit:
        ts = np.sort(np.array([a.t_s for a in r.assessments()], float))
        if len(ts) > 1:
            intervals.extend(np.diff(ts).tolist())
        for c in r.command_results():
            if c.accepted and c.decision_t_s is not None and c.acked_at_s is not None:
                latencies.append(c.acked_at_s - c.decision_t_s)
    notes: list[str] = []
    iv = np.array(intervals, float)
    la = np.array(latencies, float)
    p50 = float(np.percentile(iv, 50)) if len(iv) else None
    p99 = float(np.percentile(iv, 99)) if len(iv) else None
    frac = float((iv <= ASSESS_INTERVAL_S).mean()) if len(iv) else None
    lat99 = float(np.percentile(la, 99)) if len(la) else None
    latmax = float(la.max()) if len(la) else None
    if not len(iv):
        notes.append("no assessment intervals in audit logs")
    if not len(la):
        notes.append("no acknowledged commands with decision_t_s; latency not assessed")
    assess_ok = None if p99 is None else p99 <= ASSESS_INTERVAL_S
    ack_ok = None if latmax is None else latmax <= ACK_LIMIT_S
    # ASSUMPTION: met when p99 assessment interval <= 1 s and max decision->ack <= 2 s. With no
    # commands at all, only the assessment part decides.
    parts = [v for v in (assess_ok, ack_ok) if v is not None]
    return CriterionResult(
        criterion="Response time",
        value=p99,
        target="assessment interval p99 <= 1 s; decision->ack max <= 2 s",
        meets_target=all(parts) if parts else None,
        supported_95=None,
        n=len(iv),
        notes=notes,
        data_source=sources_of(with_audit),
        details={
            "unit": "s (p99 assessment interval)",
            "interval_p50_s": p50,
            "interval_p99_s": p99,
            "fraction_interval_le_1s": frac,
            "ack_latency_p99_s": lat99,
            "ack_latency_max_s": latmax,
            "n_commands": len(la),
            "n_runs": len(with_audit),
        },
    )


# ---------------------------------------------------------------- cost, install


def hardware_cost(bom_csv: str | Path) -> CriterionResult:
    """BOM CSV with columns item, qty, unit_cost_cad, url. Target total < 400 CAD."""
    total = 0.0
    lines = 0
    notes: list[str] = []
    with open(bom_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            qty = float(row.get("qty") or 0)
            unit = row.get("unit_cost_cad")
            if unit in (None, ""):
                notes.append(f"no unit cost for {row.get('item', '?')}")
                continue
            total += qty * float(unit)
            lines += 1
    return CriterionResult(
        criterion="Hardware cost",
        value=round(total, 2),
        target="< 400 CAD per printer",
        meets_target=total < COST_LIMIT_CAD,
        supported_95=None,
        n=lines,
        notes=notes,
        data_source="bill of materials",
        details={"unit": "CAD", "lines": lines},
    )


def install_time(install_csv: str | Path) -> CriterionResult:
    """CSV with a ``minutes`` column (one row per trial). Met when every trial is < 180 min."""
    mins: list[float] = []
    with open(install_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("minutes") not in (None, ""):
                mins.append(float(row["minutes"]))
    n = len(mins)
    # ASSUMPTION: "under 3 hours" must hold for every timed trial; mean reported too.
    return CriterionResult(
        criterion="Installation",
        value=max(mins) if mins else None,
        target="every trial < 180 min",
        meets_target=(max(mins) < INSTALL_LIMIT_MIN) if mins else None,
        supported_95=None,
        n=n,
        notes=[] if mins else ["no install trials"],
        data_source="timed install trials",
        details={"unit": "min (longest trial)", "mean_min": float(np.mean(mins)) if mins else None},
    )


def run_criteria(runs: Sequence[LoadedRun], seed: int = 0) -> list[CriterionResult]:
    """All run-based criteria, in Table 2 order."""
    return [
        lead_time(runs),
        recovery_rate(runs),
        false_alarms(runs),
        filament_waste(runs, seed=seed),
        part_quality(runs),
        response_time(runs),
    ]
