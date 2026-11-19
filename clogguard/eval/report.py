"""Table 2 report (Markdown + JSON).

Runs from different data sources are never mixed: more than one source raises
:class:`MixedSourceError` unless ``allow_mixed`` is set, in which case there is one section per
source. Any section whose source is not ``real`` carries a not-a-performance-claim label in its
heading and on every row.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clogguard.eval.criteria import (
    CriterionResult,
    hardware_cost,
    install_time,
    run_criteria,
)
from clogguard.eval.runs import LoadedRun

LABELS = {
    "simulated": "SIMULATED - not a performance claim",
    "open_dataset": "OPEN DATASET - not a performance claim",
}


class MixedSourceError(ValueError):
    pass


def source_label(source: str) -> str:
    """'' for real data; otherwise the not-a-performance-claim label."""
    if source == "real":
        return ""
    return LABELS.get(source, f"{source.upper()} - not a performance claim")


@dataclass
class Section:
    data_source: str
    n_runs: int
    results: list[CriterionResult]

    @property
    def label(self) -> str:
        return source_label(self.data_source)


@dataclass
class Table2Report:
    sections: list[Section]
    extra: list[CriterionResult] = field(default_factory=list)  # cost, install (not run-based)
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    @property
    def title(self) -> str:
        labels = [s.label for s in self.sections if s.label]
        base = "ClogGuard Table 2 report"
        if labels:
            return f"{base} - " + "; ".join(dict.fromkeys(labels))
        return f"{base} - real printer data"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "generated_at": self.generated_at,
            "sections": [
                {
                    "data_source": s.data_source,
                    "label": s.label,
                    "n_runs": s.n_runs,
                    "results": [
                        {**r.to_dict(), "label": s.label} for r in s.results
                    ],
                }
                for s in self.sections
            ],
            "not_run_based": [r.to_dict() for r in self.extra],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_markdown(self) -> str:
        out = [f"# {self.title}", "", f"Generated {self.generated_at}.", ""]
        for s in self.sections:
            head = f"## Data source: {s.data_source}"
            if s.label:
                head += f" ({s.label})"
            out += [head, "", f"{s.n_runs} runs.", ""]
            out += _table(s.results, s.label)
        if self.extra:
            out += ["## Not run-based (bill of materials, install trials)", ""]
            out += _table(self.extra, "")
        out += [
            "Bounds are exact one-sided 95% Clopper-Pearson; Wilson intervals are for display.",
            "",
        ]
        return "\n".join(out)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _table(results: Sequence[CriterionResult], label: str) -> list[str]:
    lines = [
        "| Criterion | Value | Unit | Target | Meets target | Supported 95% | n | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        name = f"{r.criterion} [{label}]" if label else r.criterion
        notes = "; ".join(r.notes).replace("|", "/")
        unit = str(r.details.get("unit", "fraction"))
        lines.append(
            f"| {name} | {_fmt(r.value)} | {unit} | {r.target} | {_fmt(r.meets_target)} | "
            f"{_fmt(r.supported_95)} | {r.n} | {notes} |"
        )
    lines.append("")
    return lines


def build_table2(
    runs: Sequence[LoadedRun],
    bom_csv: str | Path | None = None,
    install_csv: str | Path | None = None,
    allow_mixed: bool = False,
    seed: int = 0,
) -> Table2Report:
    sources = sorted({r.data_source for r in runs})
    if len(sources) > 1 and not allow_mixed:
        raise MixedSourceError(
            f"runs come from more than one data source {sources}; pass allow_mixed "
            "(--allow-mixed) to report them in separate sections"
        )
    sections = []
    for src in sources:
        subset = [r for r in runs if r.data_source == src]
        sections.append(Section(src, len(subset), run_criteria(subset, seed=seed)))
    extra = []
    if bom_csv is not None:
        extra.append(hardware_cost(bom_csv))
    if install_csv is not None:
        extra.append(install_time(install_csv))
    return Table2Report(sections=sections, extra=extra)
