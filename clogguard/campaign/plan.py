"""Run plan generator: seeded, randomised run order and printable run sheets.

Defaults are the docs/plan.md test plan. Runs are generated phase by phase (development before
validation, so validation prints stay untouched until the end). Within a phase, supervised clog
runs are grouped into blocks of ``block_length`` consecutive runs (one supervised session) and
the blocks are shuffled together with the fault-free runs. Fault-free runs cycle through the
geometries in balanced numbers.
"""

from __future__ import annotations

import csv
import html
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ServiceMode = Literal["off", "watch", "act"]

PURPOSE_CODES = {
    "baseline": "BL",
    "induction_trial": "IT",
    "clog_no_intervention": "CN",
    "recovery_tuning": "RT",
    "validation_clean": "VC",
    "validation_clog": "VX",
}

FILL_FIELDS = (
    "operator",
    "spool",
    "start_time",
    "part_mass_g",
    "waste_mass_g",
    "notes",
    "supervisor_initials",
)


class MethodSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = ""
    schedule: str = ""  # human-readable schedule for the run sheet
    recoverable: bool | None = None


class PurposeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: int = Field(ge=0)
    clog: bool
    service_mode: ServiceMode
    methods: list[str] = Field(default_factory=list)  # cycled in balanced order, then shuffled


class PlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = 2027
    run_id_prefix: str = "CG"
    block_length: int = Field(default=4, ge=1)
    geometries: list[str] = Field(default_factory=lambda: ["box", "cylinder", "bracket"])
    clog_geometry: str | None = "box"  # None = clog runs also cycle the geometries
    phases: list[list[str]]
    purposes: dict[str, PurposeSpec]
    methods: dict[str, MethodSpec]

    @model_validator(mode="after")
    def _consistent(self) -> PlanConfig:
        listed = [p for ph in self.phases for p in ph]
        unknown = sorted(set(listed) - set(self.purposes))
        if unknown:
            raise ValueError(f"phases name unknown purposes: {unknown}")
        unplaced = sorted(set(self.purposes) - set(listed))
        if unplaced:
            raise ValueError(f"purposes not in any phase: {unplaced}")
        for name, p in self.purposes.items():
            if p.clog and not p.methods:
                raise ValueError(f"clog purpose {name!r} needs methods")
            bad = sorted(set(p.methods) - set(self.methods))
            if bad:
                raise ValueError(f"purpose {name!r} uses undefined methods {bad}")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> PlanConfig:
        if path is None:
            return cls.model_validate(DEFAULT_PLAN)
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def default_plan_yaml() -> str:
    return yaml.safe_dump(DEFAULT_PLAN, sort_keys=False)


# Test plan from docs/plan.md. Method severities and schedules are placeholders for the team to
# fix in the test plan before validation (docs/plan.md, Definitions: recoverable clog).
DEFAULT_PLAN: dict[str, object] = {
    "seed": 2027,
    "run_id_prefix": "CG",
    "block_length": 4,
    "geometries": ["box", "cylinder", "bracket"],
    "clog_geometry": "box",
    # ASSUMPTION: induction trials come first so methods are qualified before the
    # no-intervention and tuning runs; validation runs come last.
    "phases": [
        ["baseline", "induction_trial"],
        ["clog_no_intervention", "recovery_tuning"],
        ["validation_clean", "validation_clog"],
    ],
    # ASSUMPTION: service modes. Lead time needs watch-only clog runs (clog_no_intervention);
    # false alarms and recovery need the system acting on validation runs.
    "purposes": {
        "baseline": {"runs": 12, "clog": False, "service_mode": "watch"},
        "induction_trial": {
            "runs": 8, "clog": True, "service_mode": "watch",
            "methods": ["temp_step", "over_flow", "small_nozzle", "fan_reduce"],
        },
        "clog_no_intervention": {
            "runs": 15, "clog": True, "service_mode": "watch",
            "methods": ["temp_step", "over_flow", "small_nozzle"],
        },
        "recovery_tuning": {
            "runs": 10, "clog": True, "service_mode": "act",
            "methods": ["temp_step", "over_flow"],
        },
        "validation_clean": {"runs": 30, "clog": False, "service_mode": "act"},
        "validation_clog": {
            "runs": 30, "clog": True, "service_mode": "act",
            # ASSUMPTION: all 30 validation clogs use recoverable methods so the recovery rule
            # ("21 of 30") applies to all of them; add "contaminated" once it is qualified.
            "methods": ["temp_step", "over_flow"],
        },
    },
    # ASSUMPTION: which methods count as recoverable is an open question in docs/plan.md;
    # temp_step and over_flow are marked recoverable here, small_nozzle (fixed) is not.
    "methods": {
        "temp_step": {
            "severity": "-5 C steps (TBD)",
            "schedule": "start 5 min; -5 C every 5 min to -25 C (TBD); temperature hidden "
            "from model",
            "recoverable": True,
        },
        "over_flow": {
            "severity": "flow +10% steps (TBD)",
            "schedule": "start 5 min; +10% flow every 5 min (TBD); flow hidden from model",
            "recoverable": True,
        },
        "small_nozzle": {
            "severity": "0.3 / 0.25 / 0.2 mm (TBD)",
            "schedule": "fixed for whole print",
            "recoverable": False,
        },
        "fan_reduce": {
            "severity": "heatsink fan 50% (TBD)",
            "schedule": "start 5 min; supervised only",
            "recoverable": None,
        },
        "contaminated": {
            "severity": "degraded spool (TBD)",
            "schedule": "whole print",
            "recoverable": None,
        },
    },
}


@dataclass
class PlannedRun:
    order: int
    run_id: str
    phase: int
    purpose: str
    geometry: str
    induction_method: str
    severity: str
    schedule: str
    recoverable: bool | None
    service_mode: str
    supervised: bool
    block: str
    fill: dict[str, str] = field(default_factory=lambda: dict.fromkeys(FILL_FIELDS, ""))

    def row(self) -> dict[str, object]:
        base = {k: v for k, v in self.__dict__.items() if k != "fill"}
        base["recoverable"] = "" if self.recoverable is None else str(self.recoverable).lower()
        return {**base, **self.fill}


def _balanced(items: list[str], n: int, rng: random.Random) -> list[str]:
    out = [items[i % len(items)] for i in range(n)]
    rng.shuffle(out)
    return out


def generate_plan(cfg: PlanConfig) -> list[PlannedRun]:
    rng = random.Random(cfg.seed)
    runs: list[PlannedRun] = []
    block_no = 0
    for phase_idx, phase in enumerate(cfg.phases, start=1):
        clog: list[dict[str, object]] = []
        clean: list[dict[str, object]] = []
        for purpose in phase:
            spec = cfg.purposes[purpose]
            if spec.clog:
                methods = _balanced(spec.methods, spec.runs, rng)
                geoms = (
                    [cfg.clog_geometry] * spec.runs
                    if cfg.clog_geometry
                    else _balanced(cfg.geometries, spec.runs, rng)
                )
                for m, g in zip(methods, geoms, strict=True):
                    clog.append({"purpose": purpose, "method": m, "geometry": g})
            else:
                for g in _balanced(cfg.geometries, spec.runs, rng):
                    clean.append({"purpose": purpose, "method": "none", "geometry": g})
        rng.shuffle(clog)
        units: list[list[dict[str, object]]] = []
        for i in range(0, len(clog), cfg.block_length):
            units.append(clog[i : i + cfg.block_length])
        units += [[c] for c in clean]
        rng.shuffle(units)
        for unit in units:
            is_block = unit[0]["method"] != "none"
            if is_block:
                block_no += 1
            for item in unit:
                purpose = str(item["purpose"])
                method = str(item["method"])
                mspec = cfg.methods.get(method, MethodSpec())
                order = len(runs) + 1
                code = PURPOSE_CODES.get(purpose, "XX")
                runs.append(
                    PlannedRun(
                        order=order,
                        run_id=f"{cfg.run_id_prefix}-{order:03d}-{code}",
                        phase=phase_idx,
                        purpose=purpose,
                        geometry=str(item["geometry"]),
                        induction_method=method,
                        severity=mspec.severity if is_block else "",
                        schedule=mspec.schedule if is_block else "",
                        recoverable=mspec.recoverable if is_block else None,
                        service_mode=cfg.purposes[purpose].service_mode,
                        supervised=is_block,
                        block=f"B{block_no:02d}" if is_block else "",
                    )
                )
    return runs


# ---------------------------------------------------------------- outputs


def write_csv(runs: list[PlannedRun], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r.row() for r in runs]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["run_id"])
        w.writeheader()
        w.writerows(rows)


def _sheet_items(r: PlannedRun) -> list[tuple[str, str]]:
    return [
        ("Run ID", r.run_id),
        ("Order / phase", f"{r.order} / {r.phase}"),
        ("Purpose", r.purpose),
        ("Geometry", r.geometry),
        ("Induction method", r.induction_method),
        ("Severity", r.severity or "-"),
        ("Schedule", r.schedule or "-"),
        ("Recoverable (test plan)", "-" if r.recoverable is None else str(r.recoverable)),
        ("Service mode", r.service_mode),
        ("Supervised block", r.block or "no (fault-free)"),
    ]


def sheet_markdown(r: PlannedRun) -> str:
    lines = [f"# Run sheet {r.run_id}", ""]
    if r.supervised:
        lines += ["**Clog run: a team member must be present for the whole print.**", ""]
    lines += ["| Field | Value |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in _sheet_items(r)]
    lines += ["", "## To fill in", "", "| Field | Entry |", "| --- | --- |"]
    lines += [f"| {f.replace('_', ' ')} | |" for f in FILL_FIELDS]
    lines += ["", "Pre-run safety checklist completed: [ ]", ""]
    return "\n".join(lines)


_CSS = (
    "body{font-family:system-ui,sans-serif;color:#222;max-width:780px;margin:2em auto}"
    "table{border-collapse:collapse;width:100%;margin-bottom:1em}"
    "td,th{border:1px solid #bbb;padding:6px 8px;text-align:left}"
    "td.fill{height:2em}.warn{font-weight:600;border:2px solid #222;padding:6px}"
    ".sheet{page-break-after:always}"
)


def sheet_html_fragment(r: PlannedRun) -> str:
    e = html.escape
    rows = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in _sheet_items(r))
    fill = "".join(
        f"<tr><th>{e(f.replace('_', ' '))}</th><td class='fill'></td></tr>" for f in FILL_FIELDS
    )
    warn = (
        "<p class='warn'>Clog run: a team member must be present for the whole print.</p>"
        if r.supervised
        else ""
    )
    return (
        f"<section class='sheet'><h1>Run sheet {e(r.run_id)}</h1>{warn}<table>{rows}</table>"
        f"<h2>To fill in</h2><table>{fill}</table>"
        "<p>Pre-run safety checklist completed: &#9744;</p></section>"
    )


def sheet_html(runs: list[PlannedRun], title: str = "ClogGuard run sheets") -> str:
    body = "".join(sheet_html_fragment(r) for r in runs)
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


def write_outputs(runs: list[PlannedRun], out_dir: str | Path) -> dict[str, Path]:
    """plan.csv, plan.md (overview), run_sheets.html (all, printable), sheets/<id>.md|.html."""
    out = Path(out_dir)
    sheets = out / "sheets"
    sheets.mkdir(parents=True, exist_ok=True)
    write_csv(runs, out / "plan.csv")
    overview = [
        "# ClogGuard run plan",
        "",
        "| # | Run ID | Phase | Purpose | Geometry | Method | Mode | Block |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    overview += [
        f"| {r.order} | {r.run_id} | {r.phase} | {r.purpose} | {r.geometry} | "
        f"{r.induction_method} | {r.service_mode} | {r.block} |"
        for r in runs
    ]
    (out / "plan.md").write_text("\n".join(overview) + "\n", encoding="utf-8")
    (out / "run_sheets.html").write_text(sheet_html(runs), encoding="utf-8")
    for r in runs:
        (sheets / f"{r.run_id}.md").write_text(sheet_markdown(r), encoding="utf-8")
        (sheets / f"{r.run_id}.html").write_text(sheet_html([r], r.run_id), encoding="utf-8")
    return {
        "csv": out / "plan.csv",
        "overview": out / "plan.md",
        "sheets_html": out / "run_sheets.html",
        "sheets_dir": sheets,
    }
