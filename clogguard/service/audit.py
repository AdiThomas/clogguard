"""Audit log and run-directory handling for the service.

Run directory: ``<runs_dir>/<run_id>/audit.jsonl``, one ``AuditRecord`` per line, ``seq``
strictly increasing from 0 (continuing after existing lines if the file already exists).

run_id: ``make_run_id(started_at, filename)`` -> ``YYYYmmddTHHMMSS_<gcode stem>`` (same
signature and format as the logger's). The runtime takes ``run_id_fn`` and ``match_run_fn``
arguments so the logger's versions can be swapped in.

Reuse rule (logger already created a run dir for this print): a directory under ``runs_dir``
whose ``meta.json`` parses as ``RunMetadata``, whose ``gcode_file`` has the same base name as
the printing file, and whose ``started_at`` is within ``MATCH_WINDOW_S`` of the service's print
start wall time. The closest match wins and its ``meta.run_id`` is used. The match is made at
print start only; a logger that creates its directory later is not picked up.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

from clogguard.core.commands import PRINTER_KINDS
from clogguard.core.events import AuditEvent, AuditRecord, CommandResult
from clogguard.core.schema import AUDIT_FILE, META_FILE, ActionRecord, RunMetadata

log = logging.getLogger(__name__)

MATCH_WINDOW_S = 10.0  # ASSUMPTION: logger and service see print start within 10 s


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _stem(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).name


def make_run_id(started_at: datetime, filename: str) -> str:
    """``YYYYMMDDTHHMMSS_<gcode stem>``. Same signature and format as the logger's
    ``clogguard.logger.run_logger.make_run_id`` so the two can be swapped at merge."""
    stem = PurePosixPath(_stem(filename)).stem if filename else ""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-.") or "print"
    return f"{started_at:%Y%m%dT%H%M%S}_{safe}"[:80]


def find_logger_run(
    runs_dir: Path, filename: str, start_wall: datetime, window_s: float = MATCH_WINDOW_S
) -> tuple[str, Path] | None:
    """Return ``(run_id, run_dir)`` of a logger-created run for this print, if any."""
    if not runs_dir.is_dir() or not filename:
        return None
    want, start = _stem(filename), _utc(start_wall)
    best: tuple[float, str, Path] | None = None
    for meta_path in runs_dir.glob(f"*/{META_FILE}"):
        try:
            meta = RunMetadata.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not meta.gcode_file or _stem(meta.gcode_file) != want:
            continue
        diff = abs((_utc(meta.started_at) - start).total_seconds())
        if diff <= window_s and (best is None or diff < best[0]):
            best = (diff, meta.run_id, meta_path.parent)
    return None if best is None else (best[1], best[2])


class AuditWriter:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / AUDIT_FILE
        self.run_id = run_id
        self.seq = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                self.seq = sum(1 for line in fh if line.strip())
        self._fh: IO[str] | None = self.path.open("a", encoding="utf-8")

    def write(self, event: AuditEvent) -> AuditRecord:
        rec = AuditRecord(seq=self.seq, run_id=self.run_id, event=event)
        self.seq += 1
        if self._fh is not None:
            self._fh.write(rec.model_dump_json() + "\n")
            self._fh.flush()
        return rec

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_audit(path: Path) -> list[AuditRecord]:
    with path.open(encoding="utf-8") as fh:
        return [AuditRecord.model_validate_json(line) for line in fh if line.strip()]


def action_record(r: CommandResult) -> ActionRecord:
    detail: dict[str, Any] = {
        k: v for k, v in r.command.model_dump().items()
        if k != "kind" and isinstance(v, int | float | str | bool)
    }
    detail["acked"] = r.acked_at_s is not None
    return ActionRecord(t_s=r.t_s, kind=r.command.kind, detail=detail, accepted=r.accepted)


def update_meta(
    run_dir: Path,
    *,
    mode: str,
    alert_time_s: float | None,
    results: Iterable[CommandResult],
) -> bool:
    """Write service fields into an existing ``meta.json``. Returns False if there is none or
    it does not parse (left untouched)."""
    path = run_dir / META_FILE
    if not path.exists():
        return False
    try:
        meta = RunMetadata.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        log.warning("meta.json in %s does not parse; not updated: %s", run_dir, exc)
        return False
    actions = [action_record(r) for r in results if r.command.kind in PRINTER_KINDS]
    meta = meta.model_copy(
        update={
            "service_active": True,
            "service_mode": mode,
            "alert_time_s": alert_time_s,
            "actions": [*meta.actions, *actions],
        }
    )
    data = RunMetadata.model_validate(meta.model_dump())  # re-validate before writing
    path.write_text(json.dumps(data.model_dump(mode="json"), indent=2), encoding="utf-8")
    return True
