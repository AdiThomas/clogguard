"""Per-run HTML report: signals over time with alert, defect onset and actions marked, plus the
run's masses, ratings and actions. Figures are matplotlib PNGs embedded as base64.
"""

from __future__ import annotations

import base64
import html
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from clogguard.core.schema import CANDIDATE_SIGNALS, CONTEXT_CHANNELS
from clogguard.eval.report import source_label
from clogguard.eval.runs import LoadedRun, load_run

SERIES = "#2a78d6"
ALERT = "#eb6834"
ONSET = "#333333"
ACTION = "#1baf7a"
GRID = "#e5e5e5"
MAX_PANELS = 8
MAX_POINTS = 3000


def _png(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _channels(frame: pd.DataFrame) -> list[str]:
    numeric = [
        c for c in frame.columns if c != "t" and pd.api.types.is_numeric_dtype(frame[c])
    ]
    signals = [c for c in numeric if c in CANDIDATE_SIGNALS or c.startswith("accel_band_")]
    context = [c for c in numeric if c in CONTEXT_CHANNELS and c not in signals]
    rest = [c for c in numeric if c not in signals and c not in context]
    return (signals + context + rest)[:MAX_PANELS]


def _events(run: LoadedRun) -> tuple[float | None, float | None, list[tuple[float, str]]]:
    actions = [(c.t_s, c.command.kind) for c in run.command_results() if c.accepted]
    if not actions:
        actions = [(a.t_s, a.kind) for a in run.meta.actions if a.accepted]
    return run.alert_time_s(), run.meta.defect_onset_s, actions


def _mark(ax: plt.Axes, alert: float | None, onset: float | None,
          actions: list[tuple[float, str]], legend: bool) -> None:
    for i, (t, _k) in enumerate(actions):
        ax.axvline(t / 60, color=ACTION, lw=1, ls=":", label="action" if i == 0 else None)
    if alert is not None:
        ax.axvline(alert / 60, color=ALERT, lw=2, ls="--", label="alert")
    if onset is not None:
        ax.axvline(onset / 60, color=ONSET, lw=2, label="defect onset")
    if legend and (actions or alert is not None or onset is not None):
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8, frameon=False, ncol=3)


def _style(ax: plt.Axes) -> None:
    ax.grid(True, color=GRID, lw=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8)


def signals_figure(run: LoadedRun) -> str | None:
    frame = run.frame
    chans = _channels(frame) if "t" in frame.columns else []
    if not chans or frame.empty:
        return None
    step = max(1, len(frame) // MAX_POINTS)
    f = frame.iloc[::step]
    alert, onset, actions = _events(run)
    fig, axes = plt.subplots(len(chans), 1, figsize=(9, 1.7 * len(chans) + 0.6), sharex=True,
                             squeeze=False)
    for i, (ax, c) in enumerate(zip(axes[:, 0], chans, strict=True)):
        ax.plot(f["t"] / 60, f[c], color=SERIES, lw=1.2)
        ax.set_ylabel(c, fontsize=8, rotation=0, ha="right", va="center")
        _style(ax)
        _mark(ax, alert, onset, actions, legend=i == 0)
    axes[-1, 0].set_xlabel("time since print start (min)", fontsize=9)
    return _png(fig)


def assessment_figure(run: LoadedRun) -> str | None:
    rows = [(a.t_s, a.p_clog, a.health) for a in run.assessments()]
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["t", "p_clog", "health"])
    panels = [c for c in ("p_clog", "health") if df[c].notna().any()]
    if not panels:
        return None
    alert, onset, actions = _events(run)
    fig, axes = plt.subplots(len(panels), 1, figsize=(9, 1.9 * len(panels) + 0.6), sharex=True,
                             squeeze=False)
    for i, (ax, c) in enumerate(zip(axes[:, 0], panels, strict=True)):
        ax.plot(df["t"] / 60, df[c], color=SERIES, lw=1.2)
        ax.set_ylabel(c, fontsize=8, rotation=0, ha="right", va="center")
        _style(ax)
        _mark(ax, alert, onset, actions, legend=i == 0)
    axes[-1, 0].set_xlabel("time since print start (min)", fontsize=9)
    return _png(fig)


def _kv_table(items: list[tuple[str, object]]) -> str:
    e = html.escape
    return "<table>" + "".join(
        f"<tr><th>{e(k)}</th><td>{e('-' if v is None or v == '' else str(v))}</td></tr>"
        for k, v in items
    ) + "</table>"


def build_run_report(run_dir: str | Path) -> str:
    run = load_run(run_dir)
    m = run.meta
    label = source_label(m.data_source)
    title = f"Run {m.run_id} - data source: {m.data_source}" + (f" ({label})" if label else "")
    alert, onset, actions = _events(run)
    lead = (onset - alert) if (alert is not None and onset is not None) else None
    summary = _kv_table([
        ("Purpose", m.purpose),
        ("Started", m.started_at.isoformat()),
        ("Operator", m.operator),
        ("Part / geometry", m.part),
        ("Spool / material / nozzle", f"{m.spool or '-'} / {m.material} / {m.nozzle_mm} mm"),
        ("Induction", f"{m.induction.method} {m.induction.severity}".strip()),
        ("Recoverable (test plan)", m.induction.recoverable),
        ("Manipulated variables", ", ".join(m.manipulated_variables)),
        ("Service mode", m.service_mode),
        ("Alert time (min)", None if alert is None else f"{alert / 60:.2f}"),
        ("Defect onset (min)", None if onset is None else f"{onset / 60:.2f}"),
        ("Lead time (min)", None if lead is None else f"{lead / 60:.2f}"),
        ("Outcome", m.outcome),
        ("Part mass (g)", m.part_mass_g),
        ("Reference mass (g)", m.reference_mass_g),
        ("Wasted filament (g)", m.wasted_filament_g),
        ("Software / config", f"{m.software_version or '-'} / {m.config_hash or '-'}"),
    ])
    e = html.escape
    ratings = "".join(
        f"<tr><td>{e(r.rater)}</td><td>{'no defect' if r.t_s is None else f'{r.t_s / 60:.2f}'}"
        "</td></tr>"
        for r in m.defect_ratings
    ) or "<tr><td colspan='2'>not rated</td></tr>"
    act_rows = "".join(
        f"<tr><td>{t / 60:.2f}</td><td>{e(k)}</td></tr>" for t, k in actions
    ) or "<tr><td colspan='2'>none</td></tr>"
    figs = []
    for cap, img in (("Signals", signals_figure(run)), ("Detector output", assessment_figure(run))):
        if img:
            figs.append(
                f"<h2>{cap}</h2><img alt='{cap} over time' src='data:image/png;base64,{img}'>"
            )
    banner = f"<p class='label'>{e(label)}</p>" if label else ""
    notes = f"<h2>Notes</h2><pre>{e(m.notes)}</pre>" if m.notes else ""
    css = (
        "body{font-family:system-ui,sans-serif;color:#222;max-width:960px;margin:2em auto}"
        "table{border-collapse:collapse;margin-bottom:1em}td,th{border:1px solid #ccc;"
        "padding:4px 8px;text-align:left}img{max-width:100%}"
        ".label{font-weight:700;border:2px solid #222;padding:6px}"
    )
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
        f"<style>{css}</style></head><body><h1>{e(title)}</h1>{banner}{summary}"
        f"{''.join(figs)}<h2>Defect ratings (blind)</h2><table><tr><th>Rater</th>"
        f"<th>Onset (min)</th></tr>{ratings}</table><h2>Actions</h2><table><tr><th>t (min)</th>"
        f"<th>Command</th></tr>{act_rows}</table>{notes}</body></html>"
    )


def write_run_report(run_dir: str | Path, out: str | Path | None = None) -> Path:
    out_path = Path(out) if out else Path(run_dir) / "report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_run_report(run_dir), encoding="utf-8")
    return out_path
