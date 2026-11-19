"""Pre-run safety checklist (docs/plan.md, Constraints).

Interactive by default; ``--yes`` accepts every confirmation and prints them. Completion is
appended to the run's ``meta.json`` notes when the run directory already has one; otherwise a
``checklist.json`` is written into the run directory for the logger to pick up.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clogguard.core.schema import META_FILE, RunMetadata

CHECKLIST_FILE = "checklist.json"


@dataclass(frozen=True)
class Item:
    key: str
    text: str
    clog_only: bool = False


ITEMS: tuple[Item, ...] = (
    Item("thermal_runaway", "Klipper thermal-runaway protection (verify_heater) is active and "
         "unchanged; ClogGuard cannot disable it."),
    Item("max_temp", "Klipper [extruder] max_temp equals the material limit used by the "
         "ClogGuard service (temperature clamp)."),
    Item("datasheet", "Filament datasheet maximum temperature is recorded below."),
    Item("supervisor", "A team member (supervisor) is present for the whole print.",
         clog_only=True),
    Item("wiring", "Added low-voltage DC wiring inspected: no exposed conductors, strain "
         "relief in place, no mains modification."),
    Item("fire", "Fire safety: extinguisher within reach, smoke detector working, nothing "
         "flammable around the printer."),
)


class ChecklistError(RuntimeError):
    pass


def items_for(clog_run: bool) -> list[Item]:
    return [i for i in ITEMS if clog_run or not i.clog_only]


def checklist_markdown(clog_run: bool = True) -> str:
    lines = ["# ClogGuard pre-run safety checklist", ""]
    for i in items_for(clog_run):
        suffix = " (clog runs)" if i.clog_only else ""
        lines.append(f"- [ ] {i.text}{suffix}")
    lines += [
        "",
        "Filament datasheet max temperature (C): ________",
        "Klipper max_temp (C): ________",
        "Supervisor: ________   Operator: ________   Date/time: ________",
        "",
        "Do not start the print unless every box is ticked.",
        "",
    ]
    return "\n".join(lines)


@dataclass
class ChecklistRecord:
    run_id: str
    completed_at: str
    confirmed: list[str]
    datasheet_max_c: float
    klipper_max_temp_c: float | None
    supervisor: str
    operator: str
    clog_run: bool
    mode: str  # "interactive" or "yes"

    def note(self) -> str:
        kmax = "-" if self.klipper_max_temp_c is None else f"{self.klipper_max_temp_c:g}"
        return (
            f"[pre-run checklist {self.completed_at} ({self.mode})] confirmed: "
            f"{', '.join(self.confirmed)}; datasheet_max_c={self.datasheet_max_c:g}; "
            f"klipper_max_temp_c={kmax}; "
            f"supervisor={self.supervisor or '-'}; operator={self.operator or '-'}"
        )


def run_checklist(
    run_id: str,
    clog_run: bool,
    datasheet_max_c: float | None,
    klipper_max_temp_c: float | None = None,
    supervisor: str = "",
    operator: str = "",
    assume_yes: bool = False,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> ChecklistRecord:
    """Walk the checklist. Raises ChecklistError on any refusal or missing value."""
    if datasheet_max_c is None:
        if assume_yes:
            raise ChecklistError("--yes needs --datasheet-max-c")
        raw = ask("Filament datasheet max temperature (C): ").strip()
        try:
            datasheet_max_c = float(raw)
        except ValueError as exc:
            raise ChecklistError(f"not a temperature: {raw!r}") from exc
    if klipper_max_temp_c is not None and abs(klipper_max_temp_c - datasheet_max_c) > 1e-6:
        # ASSUMPTION: Klipper max_temp must equal the datasheet limit (docs/plan.md hard limits:
        # Klipper max_temp set to the same value as the service clamp).
        raise ChecklistError(
            f"Klipper max_temp {klipper_max_temp_c:g} C differs from the material limit "
            f"{datasheet_max_c:g} C"
        )
    if clog_run and not supervisor:
        if assume_yes:
            raise ChecklistError("clog runs need --supervisor")
        supervisor = ask("Supervisor name: ").strip()
        if not supervisor:
            raise ChecklistError("clog runs need a supervisor present")
    confirmed: list[str] = []
    for item in items_for(clog_run):
        if assume_yes:
            say(f"[x] {item.text}")
        else:
            ans = ask(f"{item.text} [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                raise ChecklistError(f"not confirmed: {item.key}; do not start the print")
        confirmed.append(item.key)
    return ChecklistRecord(
        run_id=run_id,
        completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        confirmed=confirmed,
        datasheet_max_c=datasheet_max_c,
        klipper_max_temp_c=klipper_max_temp_c,
        supervisor=supervisor,
        operator=operator,
        clog_run=clog_run,
        mode="yes" if assume_yes else "interactive",
    )


def record_completion(run_dir: str | Path, rec: ChecklistRecord) -> Path:
    """Append the completion note to meta.json notes if present; else write checklist.json."""
    run_dir = Path(run_dir)
    meta_path = run_dir / META_FILE
    if meta_path.is_file():
        meta = RunMetadata.model_validate_json(meta_path.read_text(encoding="utf-8"))
        notes = (meta.notes + "\n" if meta.notes else "") + rec.note()
        meta = meta.model_copy(update={"notes": notes})
        meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        return meta_path
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / CHECKLIST_FILE
    out.write_text(json.dumps({**rec.__dict__, "note": rec.note()}, indent=2), encoding="utf-8")
    return out
