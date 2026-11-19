import json
from pathlib import Path

import pytest

from clogguard.eval.cli import main
from clogguard.eval.report import MixedSourceError, build_table2
from clogguard.eval.runs import load_runs
from tests.eval.helpers import make_run


def _some(root: Path, source: str, prefix: str) -> None:
    for i in range(3):
        make_run(root, f"{prefix}c{i}", purpose="validation_clean", mode="act", source=source,
                 frame=False, assess_dt=0.5, duration=10)
    make_run(root, f"{prefix}x", purpose="clog_no_intervention", clog=True, mode="watch",
             onset=900.0, alert=500.0, source=source, frame=False, waste=10.0)


def test_mixed_refused_and_allowed(tmp_path: Path) -> None:
    _some(tmp_path, "simulated", "s")
    _some(tmp_path, "real", "r")
    runs = load_runs(tmp_path)
    with pytest.raises(MixedSourceError):
        build_table2(runs)
    rep = build_table2(runs, allow_mixed=True)
    assert [s.data_source for s in rep.sections] == ["real", "simulated"]
    md = rep.to_markdown()
    assert "## Data source: real\n" in md
    assert "SIMULATED - not a performance claim" in rep.title
    assert main(["table2", "--runs", str(tmp_path), "--out", str(tmp_path / "o.md")]) == 2


def test_simulated_label_on_every_row(tmp_path: Path) -> None:
    _some(tmp_path / "runs", "simulated", "s")
    bom = tmp_path / "bom.csv"
    bom.write_text("item,qty,unit_cost_cad,url\nx,1,100,u\n")
    inst = tmp_path / "inst.csv"
    inst.write_text("minutes\n100\n")
    out = tmp_path / "out" / "report.md"
    rc = main(["table2", "--runs", str(tmp_path / "runs"), "--bom", str(bom), "--install",
               str(inst), "--out", str(out)])
    assert rc == 0
    md = out.read_text()
    assert md.startswith("# ClogGuard Table 2 report - SIMULATED - not a performance claim")
    rows = [ln for ln in md.splitlines() if ln.startswith("| ") and "Criterion" not in ln
            and not ln.startswith("| ---")]
    run_rows = [ln for ln in rows if "Hardware cost" not in ln and "Installation" not in ln]
    assert len(run_rows) == 6
    assert all("SIMULATED - not a performance claim" in ln for ln in run_rows)
    data = json.loads(out.with_suffix(".json").read_text())
    assert all(r["label"] for s in data["sections"] for r in s["results"])
    assert [r["criterion"] for r in data["not_run_based"]] == ["Hardware cost", "Installation"]


def test_open_dataset_and_real_labels(tmp_path: Path) -> None:
    _some(tmp_path / "o", "open_dataset", "o")
    rep = build_table2(load_runs(tmp_path / "o"))
    assert "OPEN DATASET - not a performance claim" in rep.to_markdown()
    _some(tmp_path / "r", "real", "r")
    rep = build_table2(load_runs(tmp_path / "r"))
    assert rep.title.endswith("real printer data")
    assert "not a performance claim" not in rep.to_markdown()


def test_cli_no_runs(tmp_path: Path) -> None:
    assert main(["table2", "--runs", str(tmp_path)]) == 1
