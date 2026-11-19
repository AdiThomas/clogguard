import csv
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clogguard.campaign.cli import main
from clogguard.campaign.plan import DEFAULT_PLAN, PlanConfig, generate_plan


def test_default_counts_and_order() -> None:
    runs = generate_plan(PlanConfig.load())
    counts = Counter(r.purpose for r in runs)
    assert counts == {"baseline": 12, "induction_trial": 8, "clog_no_intervention": 15,
                      "recovery_tuning": 10, "validation_clean": 30, "validation_clog": 30}
    assert len({r.run_id for r in runs}) == 105
    assert [r.phase for r in runs] == sorted(r.phase for r in runs)  # validation last
    assert all(r.phase == 3 for r in runs if r.purpose.startswith("validation"))
    # supervised clog runs come in consecutive blocks of <= 4
    blocks: dict[str, list[int]] = {}
    for r in runs:
        assert r.supervised == (r.induction_method != "none")
        if r.supervised:
            blocks.setdefault(r.block, []).append(r.order)
    for orders in blocks.values():
        assert len(orders) <= 4 and orders == list(range(orders[0], orders[0] + len(orders)))
    # geometries balanced across fault-free runs of each purpose
    for purpose in ("baseline", "validation_clean"):
        g = Counter(r.geometry for r in runs if r.purpose == purpose)
        assert set(g) == {"box", "cylinder", "bracket"} and max(g.values()) - min(g.values()) <= 1
    vx = [r for r in runs if r.purpose == "validation_clog"]
    assert all(r.recoverable is True and r.service_mode == "act" for r in vx)
    assert all(r.service_mode == "watch" for r in runs if r.purpose == "clog_no_intervention")


def test_seeded_and_block_length() -> None:
    a = generate_plan(PlanConfig.load())
    b = generate_plan(PlanConfig.load())
    assert [r.row() for r in a] == [r.row() for r in b]
    cfg = PlanConfig.model_validate({**DEFAULT_PLAN, "seed": 1, "block_length": 2,
                                     "clog_geometry": None})
    c = generate_plan(cfg)
    assert [r.run_id + r.purpose for r in c] != [r.run_id + r.purpose for r in a]
    assert max(Counter(r.block for r in c if r.block).values()) == 2
    assert len({r.geometry for r in c if r.supervised}) == 3


def test_config_validation() -> None:
    bad = {**DEFAULT_PLAN, "phases": [["baseline"]]}
    with pytest.raises(ValidationError, match="not in any phase"):
        PlanConfig.model_validate(bad)
    bad = {**DEFAULT_PLAN, "phases": [*DEFAULT_PLAN["phases"], ["nope"]]}  # type: ignore[misc]
    with pytest.raises(ValidationError, match="unknown purposes"):
        PlanConfig.model_validate(bad)
    purposes = dict(DEFAULT_PLAN["purposes"])  # type: ignore[arg-type]
    purposes["baseline"] = {"runs": 1, "clog": True, "service_mode": "watch"}
    with pytest.raises(ValidationError, match="needs methods"):
        PlanConfig.model_validate({**DEFAULT_PLAN, "purposes": purposes})
    purposes["baseline"] = {"runs": 1, "clog": True, "service_mode": "watch", "methods": ["x"]}
    with pytest.raises(ValidationError, match="undefined methods"):
        PlanConfig.model_validate({**DEFAULT_PLAN, "purposes": purposes})


def test_cli_plan_outputs(tmp_path: Path) -> None:
    cfg_path = tmp_path / "plan.yaml"
    assert main(["plan", "--write-default", str(cfg_path)]) == 0
    data = yaml.safe_load(cfg_path.read_text())
    data["purposes"]["validation_clean"]["runs"] = 6
    cfg_path.write_text(yaml.safe_dump(data))
    out = tmp_path / "camp"
    assert main(["plan", "--config", str(cfg_path), "--out", str(out), "--seed", "5",
                 "--block-length", "3"]) == 0
    rows = list(csv.DictReader((out / "plan.csv").open()))
    assert len(rows) == 81
    for f in ("run_id", "purpose", "geometry", "induction_method", "schedule", "service_mode",
              "operator", "spool", "start_time", "part_mass_g", "waste_mass_g", "notes",
              "supervisor_initials"):
        assert f in rows[0]
    rid = rows[0]["run_id"]
    md = (out / "sheets" / f"{rid}.md").read_text()
    assert "supervisor initials" in md and rid in md
    html = (out / "run_sheets.html").read_text()
    assert html.count("class='sheet'") == 81 and "team member must be present" in html
    assert (out / "sheets" / f"{rid}.html").is_file() and (out / "plan.md").is_file()
