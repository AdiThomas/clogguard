import json
from pathlib import Path

import pytest

from clogguard.campaign.checklist import (
    ChecklistError,
    checklist_markdown,
    items_for,
    record_completion,
    run_checklist,
)
from clogguard.campaign.cli import main
from clogguard.campaign.run_report import build_run_report
from clogguard.core.schema import RunMetadata
from tests.eval.helpers import make_run


def test_markdown_lists_items(tmp_path: Path) -> None:
    md = checklist_markdown()
    for key in ("thermal-runaway", "max_temp", "datasheet", "supervisor", "low-voltage", "Fire"):
        assert key in md
    assert len(items_for(False)) == len(items_for(True)) - 1
    assert main(["checklist", "--markdown", str(tmp_path / "c.md")]) == 0


def test_yes_mode_records_in_meta(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    d = make_run(tmp_path, "cg1", clog=True, frame=False)
    rc = main(["checklist", "--run-dir", str(d), "--yes", "--datasheet-max-c", "220",
               "--klipper-max-temp-c", "220", "--supervisor", "AT"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("[x]") == 6
    meta = RunMetadata.model_validate_json((d / "meta.json").read_text())
    assert "pre-run checklist" in meta.notes and "datasheet_max_c=220" in meta.notes
    assert "supervisor=AT" in meta.notes


def test_yes_mode_refusals(tmp_path: Path) -> None:
    d = make_run(tmp_path, "cg1", clog=True, frame=False)
    assert main(["checklist", "--run-dir", str(d), "--yes", "--datasheet-max-c", "220"]) == 1
    assert main(["checklist", "--run-dir", str(d), "--yes", "--supervisor", "A"]) == 1
    assert main(["checklist", "--run-dir", str(d), "--yes", "--supervisor", "A",
                 "--datasheet-max-c", "220", "--klipper-max-temp-c", "230"]) == 1
    assert main(["checklist"]) == 2


def test_interactive(tmp_path: Path) -> None:
    answers = iter(["215", "Sam"] + ["y"] * 6)
    rec = run_checklist("x", clog_run=True, datasheet_max_c=None,
                        ask=lambda _p: next(answers), say=lambda _s: None)
    assert rec.datasheet_max_c == 215 and rec.supervisor == "Sam" and len(rec.confirmed) == 6
    answers = iter(["y", "n"])
    with pytest.raises(ChecklistError, match="max_temp"):
        run_checklist("x", clog_run=False, datasheet_max_c=220, ask=lambda _p: next(answers))
    with pytest.raises(ChecklistError, match="not a temperature"):
        run_checklist("x", clog_run=False, datasheet_max_c=None, ask=lambda _p: "hot")
    with pytest.raises(ChecklistError, match="supervisor"):
        run_checklist("x", clog_run=True, datasheet_max_c=220, ask=lambda _p: "")
    # no meta.json yet: checklist.json written
    p = record_completion(tmp_path / "new", rec)
    assert p.name == "checklist.json" and json.loads(p.read_text())["run_id"] == "x"


def test_run_report(tmp_path: Path) -> None:
    d = make_run(tmp_path, "cg2", purpose="validation_clog", clog=True, mode="act",
                 onset=40.0, alert=20.0, part_mass=25.0, waste=3.5, assess_dt=1.0,
                 commands=[(25.0, "set_speed_factor")])
    html = build_run_report(d)
    assert "data source: simulated (SIMULATED - not a performance claim)" in html
    assert html.count("data:image/png;base64,") == 2
    assert "set_speed_factor" in html and "3.5" in html
    out = tmp_path / "r.html"
    assert main(["report", str(d), "--out", str(out)]) == 0 and out.is_file()
    e = make_run(tmp_path, "cg3", source="real", frame=False)
    html = build_run_report(e)
    assert "data source: real" in html and "not a performance claim" not in html
    assert "base64" not in html
