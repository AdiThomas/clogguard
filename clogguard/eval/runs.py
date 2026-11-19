"""Read-only loader of run directories (core layout).

A run directory is any directory containing ``meta.json``. It may also hold
``timeseries.parquet`` and ``audit.jsonl``. This loader never writes.

# ASSUMPTION: the run store (workstream A) is built in parallel; this small loader reads the core
# layout directly so evaluation does not depend on it. Swap for the store API at merge if wanted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from clogguard.core.commands import PRINTER_KINDS
from clogguard.core.events import Assessment, AuditRecord, CommandResult
from clogguard.core.schema import AUDIT_FILE, META_FILE, TIMESERIES_FILE, Run, RunMetadata


@dataclass(frozen=True)
class LoadedRun:
    """A run read from disk: metadata, base-rate frame (may be empty) and audit records."""

    path: Path
    meta: RunMetadata
    frame: pd.DataFrame
    audit: list[AuditRecord] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.meta.run_id

    @property
    def data_source(self) -> str:
        return self.meta.data_source

    @property
    def run(self) -> Run:
        return Run(meta=self.meta, frame=self.frame)

    def command_results(self) -> list[CommandResult]:
        return [r.event for r in self.audit if isinstance(r.event, CommandResult)]

    def assessments(self) -> list[Assessment]:
        return [r.event for r in self.audit if isinstance(r.event, Assessment)]

    def accepted_printer_commands(self) -> list[tuple[float, str]]:
        """(t_s, kind) of accepted commands that reach the printer (everything but Notify).

        Audit CommandResult events are preferred; ``meta.actions`` is used when the audit log
        holds no command results (e.g. hand-entered actions).
        """
        from_audit = [
            (c.t_s, c.command.kind)
            for c in self.command_results()
            if c.accepted and c.command.kind in PRINTER_KINDS
        ]
        if self.command_results():
            return from_audit
        return [
            (a.t_s, a.kind) for a in self.meta.actions if a.accepted and a.kind in PRINTER_KINDS
        ]

    def accepted_notifies(self) -> list[float]:
        """Times of accepted Notify commands (audit first, then meta.actions)."""
        if self.command_results():
            return [
                c.t_s for c in self.command_results() if c.accepted and c.command.kind == "notify"
            ]
        return [a.t_s for a in self.meta.actions if a.accepted and a.kind == "notify"]

    def alert_time_s(self) -> float | None:
        """``meta.alert_time_s``; else the first warning/clog assessment in the audit log."""
        if self.meta.alert_time_s is not None:
            return self.meta.alert_time_s
        # ASSUMPTION: without an explicit alert time, the first assessment in state warning or
        # clog is the alert.
        times = [a.t_s for a in self.assessments() if a.state in ("warning", "clog")]
        return min(times) if times else None


def discover_run_dirs(root: str | Path) -> list[Path]:
    """Every directory under ``root`` (inclusive) that contains ``meta.json``, sorted."""
    root = Path(root)
    if (root / META_FILE).is_file():
        return [root]
    return sorted(p.parent for p in root.rglob(META_FILE) if p.is_file())


def read_audit(path: str | Path) -> list[AuditRecord]:
    path = Path(path)
    if not path.is_file():
        return []
    out: list[AuditRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(AuditRecord.model_validate_json(line))
    return out


def load_run(run_dir: str | Path, *, with_frame: bool = True) -> LoadedRun:
    run_dir = Path(run_dir)
    meta = RunMetadata.model_validate_json((run_dir / META_FILE).read_text(encoding="utf-8"))
    ts = run_dir / TIMESERIES_FILE
    if with_frame and ts.is_file():
        frame = pd.read_parquet(ts)
        if "t" in frame.columns:
            frame = frame.sort_values("t").reset_index(drop=True)
    else:
        frame = pd.DataFrame({"t": pd.Series(dtype=float)})
    return LoadedRun(path=run_dir, meta=meta, frame=frame, audit=read_audit(run_dir / AUDIT_FILE))


def load_runs(
    roots: str | Path | Iterable[str | Path], *, with_frame: bool = True
) -> list[LoadedRun]:
    """Load every run under one or more roots. Duplicate run ids raise ValueError."""
    if isinstance(roots, str | Path):
        roots = [roots]
    runs: list[LoadedRun] = []
    seen: dict[str, Path] = {}
    for root in roots:
        for d in discover_run_dirs(root):
            r = load_run(d, with_frame=with_frame)
            if r.run_id in seen:
                raise ValueError(f"duplicate run_id {r.run_id!r}: {seen[r.run_id]} and {d}")
            seen[r.run_id] = d
            runs.append(r)
    return runs
