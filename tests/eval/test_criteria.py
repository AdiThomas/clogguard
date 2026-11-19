from pathlib import Path

import pytest

from clogguard.eval import criteria
from clogguard.eval.runs import discover_run_dirs, load_runs
from tests.eval.helpers import make_run


def test_loader_discovers_recursively(tmp_path: Path) -> None:
    make_run(tmp_path / "a", "r1")
    make_run(tmp_path / "b" / "c", "r2", frame=False, commands=[(1.0, "notify")])
    assert len(discover_run_dirs(tmp_path)) == 2
    assert discover_run_dirs(tmp_path / "a" / "r1") == [tmp_path / "a" / "r1"]
    runs = {r.run_id: r for r in load_runs(tmp_path)}
    assert len(runs["r1"].frame) == 600 and runs["r2"].frame.empty
    assert runs["r2"].accepted_notifies() == [1.0]
    assert runs["r1"].run.run_id == "r1"
    make_run(tmp_path / "d", "r1")
    with pytest.raises(ValueError, match="duplicate"):
        load_runs(tmp_path)


def _clog_watch(root: Path, i: int, lead: float | None, onset: float = 1500.0, **kw):
    alert = None if lead is None else onset - lead
    return make_run(root, f"cn{i}", purpose="clog_no_intervention", clog=True, mode="watch",
                    onset=onset, alert=alert, **kw)


def test_lead_time_pass_and_exclusions(tmp_path: Path) -> None:
    for i in range(10):
        _clog_watch(tmp_path, i, 420.0)
    make_run(tmp_path, "acted", purpose="validation_clog", clog=True, mode="act", onset=900.0,
             alert=800.0)
    make_run(tmp_path, "off", purpose="induction_trial", clog=True, mode="off", onset=900.0)
    make_run(tmp_path, "watchcmd", purpose="clog_no_intervention", clog=True, mode="watch",
             onset=900.0, alert=100.0, commands=[(200.0, "set_speed_factor")])
    make_run(tmp_path, "norate", purpose="clog_no_intervention", clog=True, mode="watch",
             rated=False, alert=100.0)
    make_run(tmp_path, "clean", purpose="baseline", mode="watch")
    r = criteria.lead_time(load_runs(tmp_path, with_frame=False))
    assert r.n == 10 and r.value == pytest.approx(7.0)
    assert r.meets_target is True
    assert r.details["fraction_ge_5min"] == 1.0
    # 10/10: lower bound 0.741 < 0.90 -> not supported at this n
    assert r.supported_95 is False
    ex = r.details["excluded"]
    assert set(ex) == {"acted", "off", "watchcmd", "norate"}
    assert "acted" in ex["acted"]
    assert r.data_source == "simulated"


def test_lead_time_fail_and_misses(tmp_path: Path) -> None:
    _clog_watch(tmp_path, 0, None)  # no alert -> 0
    _clog_watch(tmp_path, 1, -60.0)  # late alert -> negative
    _clog_watch(tmp_path, 2, 600.0)
    r = criteria.lead_time(load_runs(tmp_path))
    assert r.details["lead_s"] == {"cn0": 0.0, "cn1": -60.0, "cn2": 600.0}
    assert r.details["misses"] == ["cn0", "cn1"]
    assert r.meets_target is False and r.supported_95 is False


def test_lead_time_supported(tmp_path: Path) -> None:
    for i in range(30):
        _clog_watch(tmp_path, i, 400.0)
    r = criteria.lead_time(load_runs(tmp_path, with_frame=False))
    assert r.supported_95 is True  # 30/30: lower bound 0.905


def test_lead_time_alert_from_audit(tmp_path: Path) -> None:
    from clogguard.core.events import Assessment, AuditRecord
    from tests.eval.helpers import T0

    d = _clog_watch(tmp_path, 0, None, frame=False)
    recs = [AuditRecord(seq=0, run_id="cn0",
                        event=Assessment(t_s=1000.0, wall_time=T0, state="warning"))]
    (d / "audit.jsonl").write_text(recs[0].model_dump_json() + "\n")
    r = criteria.lead_time(load_runs(tmp_path))
    assert r.details["lead_s"]["cn0"] == 500.0


def _recovery(root: Path, ok: int, bad: int) -> None:
    for i in range(ok + bad):
        make_run(root, f"vx{i}", purpose="validation_clog", clog=True, mode="act",
                 recoverable=True, onset=None if i < ok else 900.0, frame=False)


@pytest.mark.parametrize(("ok", "bad", "meets", "supp"), [
    (26, 4, True, True), (25, 5, True, False), (21, 9, True, False), (20, 10, False, False),
    (18, 2, True, True)])
def test_recovery(tmp_path: Path, ok: int, bad: int, meets: bool, supp: bool) -> None:
    _recovery(tmp_path, ok, bad)
    make_run(tmp_path, "nonrec", purpose="validation_clog", clog=True, mode="act",
             recoverable=False, onset=900.0, frame=False)
    make_run(tmp_path, "watch", purpose="validation_clog", clog=True, mode="watch",
             recoverable=True, onset=900.0, frame=False)
    r = criteria.recovery_rate(load_runs(tmp_path, with_frame=False))
    assert r.n == ok + bad
    assert r.meets_target is meets and r.supported_95 is supp


def test_recovery_unrated_excluded(tmp_path: Path) -> None:
    make_run(tmp_path, "u", purpose="validation_clog", clog=True, mode="act", recoverable=True,
             rated=False, frame=False)
    r = criteria.recovery_rate(load_runs(tmp_path))
    assert r.n == 0 and r.meets_target is None and "no defect ratings" in r.notes[0]


def test_false_alarms_counting(tmp_path: Path) -> None:
    for i in range(30):
        cmds = None
        if i == 0:
            cmds = [(100.0, "set_speed_factor"), (200.0, "pause")]
        elif i == 1:
            cmds = [(100.0, "notify")]
        make_run(tmp_path, f"vc{i}", purpose="validation_clean", mode="act", commands=cmds,
                 frame=False)
    # intervention recorded only in meta.actions (no audit)
    from clogguard.core.schema import ActionRecord
    make_run(tmp_path, "vcmeta", purpose="validation_clean", mode="act", frame=False,
             actions=[ActionRecord(t_s=5.0, kind="set_nozzle_temp")])
    make_run(tmp_path, "vcwatch", purpose="validation_clean", mode="watch", alert=50.0,
             frame=False)
    make_run(tmp_path, "rejected", purpose="validation_clean", mode="act", frame=False,
             actions=[ActionRecord(t_s=5.0, kind="pause", accepted=False)])
    r = criteria.false_alarms(load_runs(tmp_path))
    assert r.n == 33
    assert r.details["runs_with_intervention"] == 2
    assert r.details["interventions_total"] == 3
    assert r.details["false_alert_runs"] == ["vc1", "vcwatch"]
    assert r.meets_target is True and r.supported_95 is False


def test_false_alarms_pass_supported_and_fail(tmp_path: Path) -> None:
    for i in range(30):
        make_run(tmp_path / "a", f"c{i}", purpose="validation_clean", mode="act", frame=False)
    r = criteria.false_alarms(load_runs(tmp_path / "a"))
    assert r.meets_target is True and r.supported_95 is True
    for i in range(30):
        make_run(tmp_path / "b", f"c{i}", purpose="validation_clean", mode="act", frame=False,
                 commands=[(10.0, "pause")] if i < 4 else None)
    r = criteria.false_alarms(load_runs(tmp_path / "b"))
    assert r.meets_target is False and r.supported_95 is False


def _waste(root: Path, sys_vals: list[float], base_vals: list[float]) -> None:
    for i, w in enumerate(sys_vals):
        make_run(root, f"s{i}", purpose="validation_clog", clog=True, mode="act", waste=w,
                 frame=False)
    for i, w in enumerate(base_vals):
        make_run(root, f"b{i}", purpose="clog_no_intervention", clog=True, mode="watch",
                 waste=w, frame=False)


def test_waste_pass(tmp_path: Path) -> None:
    _waste(tmp_path, [2.0, 3, 2.5, 1.5, 2, 3, 2.2, 1.8], [10.0, 12, 9, 11, 14, 10, 13, 9.5])
    r = criteria.filament_waste(load_runs(tmp_path))
    assert r.value == pytest.approx(1 - 2.25 / 11.0625)
    assert r.meets_target is True and r.supported_95 is True
    lo, hi = r.details["reduction_ci95"]
    assert lo < r.value < hi


def test_waste_fail(tmp_path: Path) -> None:
    _waste(tmp_path, [8.0, 9, 7, 10, 8.5], [10.0, 12, 9, 11, 14])
    r = criteria.filament_waste(load_runs(tmp_path))
    assert r.meets_target is False and r.supported_95 is False


def test_waste_missing(tmp_path: Path) -> None:
    _waste(tmp_path, [1.0], [])
    r = criteria.filament_waste(load_runs(tmp_path))
    assert r.value is None and r.meets_target is None


def test_part_quality(tmp_path: Path) -> None:
    kw = dict(purpose="validation_clog", clog=True, mode="act", recoverable=True, frame=False)
    make_run(tmp_path / "p", "a", part_mass=25.5, reference_mass=25.0, **kw)
    make_run(tmp_path / "p", "b", part_mass=24.0, **kw)  # uses reference_part fallback (25)
    make_run(tmp_path / "p", "ref", purpose="reference_part", part_mass=25.0, frame=False)
    make_run(tmp_path / "p", "defect", part_mass=10.0, reference_mass=25.0, onset=100.0,
             **{k: v for k, v in kw.items()})
    r = criteria.part_quality(load_runs(tmp_path / "p"))
    assert r.n == 2 and r.meets_target is True
    assert r.details["deviation_by_run"]["b"] == pytest.approx(-0.04)
    make_run(tmp_path / "f", "c", part_mass=23.0, reference_mass=25.0, **kw)
    make_run(tmp_path / "f", "nomass", reference_mass=25.0, **kw)
    r = criteria.part_quality(load_runs(tmp_path / "f"))
    assert r.n == 1 and r.meets_target is False and "nomass" in r.notes[0]


def test_response_time_pass_and_fail(tmp_path: Path) -> None:
    make_run(tmp_path / "ok", "a", mode="act", assess_dt=0.5, commands=[(10.0, "pause")])
    r = criteria.response_time(load_runs(tmp_path / "ok", with_frame=False))
    assert r.meets_target is True
    assert r.details["interval_p50_s"] == pytest.approx(0.5)
    assert r.details["fraction_interval_le_1s"] == 1.0
    assert r.details["ack_latency_max_s"] == pytest.approx(0.3)
    make_run(tmp_path / "slow", "b", mode="act", assess_dt=1.5)
    r = criteria.response_time(load_runs(tmp_path / "slow"))
    assert r.meets_target is False and any("latency not assessed" in n for n in r.notes)
    make_run(tmp_path / "lag", "c", mode="act", assess_dt=0.5, commands=[(10.0, "pause")],
             ack_latency=2.5)
    r = criteria.response_time(load_runs(tmp_path / "lag"))
    assert r.meets_target is False
    make_run(tmp_path / "none", "d")
    r = criteria.response_time(load_runs(tmp_path / "none"))
    assert r.meets_target is None


def test_cost_and_install(tmp_path: Path) -> None:
    bom = tmp_path / "bom.csv"
    bom.write_text("item,qty,unit_cost_cad,url\nload cell,1,25.50,x\nhx711,2,5,y\nbox,1,,z\n")
    r = criteria.hardware_cost(bom)
    assert r.value == pytest.approx(35.5) and r.meets_target is True and r.notes
    bom.write_text("item,qty,unit_cost_cad,url\npi,1,400,x\n")
    assert criteria.hardware_cost(bom).meets_target is False
    inst = tmp_path / "install.csv"
    inst.write_text("trial,minutes\n1,120\n2,150\n")
    r = criteria.install_time(inst)
    assert r.meets_target is True and r.details["mean_min"] == 135
    inst.write_text("trial,minutes\n1,120\n2,185\n")
    assert criteria.install_time(inst).meets_target is False
    inst.write_text("trial,minutes\n")
    assert criteria.install_time(inst).meets_target is None
