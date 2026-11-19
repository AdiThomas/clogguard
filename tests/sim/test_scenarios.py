from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from clogguard.core.commands import Notify, Pause, RunPurge, SetNozzleTemp, SetSpeedFactor
from clogguard.core.schema import CONTEXT_CHANNELS
from clogguard.sim.cli import main as sim_main
from clogguard.sim.io import read_run, write_run
from clogguard.sim.printjob import GEOMETRIES, generate_job
from clogguard.sim.scenarios import (
    PRESETS,
    Scenario,
    generate_run,
    preset,
    random_clog,
    random_normal,
    sim_channel_config,
)

SIGNALS = ["force_g", "extruder_power", "tmc_sg_result", "accel_rms", "encoder_slip_mm"]


def once_at(t_act: float, cmds):
    fired = [False]

    def controller(t: float, frame: pd.DataFrame):
        if not fired[0] and t >= t_act:
            fired[0] = True
            return list(cmds)
        return []

    return controller


def window(run, col: str, a: float, b: float) -> float:
    f = run.frame
    return float(f.loc[(f.t >= a) & (f.t < b), col].mean())


@pytest.mark.parametrize("geometry", GEOMETRIES)
def test_print_jobs_match_test_part(geometry):
    job = generate_job(geometry, seed=4)
    assert 30 * 60 <= job.duration_s <= 41 * 60
    assert 19.0 <= job.mass_g <= 27.0
    again = generate_job(geometry, seed=4)
    assert np.array_equal(job.e_v, again.e_v)
    assert job.e_v.min() < 0  # retractions present
    assert job.layer.max() == job.n_layers - 1


def test_determinism_by_seed():
    sc = preset("fast_viscous", seed=5)
    a, b = generate_run(sc), generate_run(sc)
    pd.testing.assert_frame_equal(a.frame, b.frame)
    assert a.meta == b.meta
    c = generate_run(preset("fast_viscous", seed=6))
    assert not np.allclose(a.frame["force_g"].iloc[:1000], c.frame["force_g"].iloc[:1000])


def test_run_schema_and_channels(baseline):
    run = baseline("slow_viscous")
    cfg = sim_channel_config()
    assert run.meta.data_source == "simulated"
    assert run.meta.induction.method == "simulated"
    assert run.meta.induction.recoverable is True
    assert run.meta.manipulated_variables == []
    assert run.meta.purpose == "clog_no_intervention"
    assert set(cfg.names()) == set(run.frame.columns) - {"t"}
    assert set(CONTEXT_CHANNELS) <= set(run.frame.columns)
    assert set(cfg.names("label")) == {"true_restriction", "true_deficit", "true_defect"}
    assert np.allclose(np.diff(run.frame.t.to_numpy()), 0.1)
    assert run.meta.defect_ratings[0].rater == "sim_truth"
    assert run.meta.defect_onset_s is not None
    assert run.meta.part_mass_g < run.meta.reference_mass_g
    # tmc is held at 1 Hz and integer-valued
    sg = run.frame.tmc_sg_result.to_numpy()
    assert np.all(sg == np.round(sg))
    assert (np.diff(sg) != 0).mean() < 0.15


def test_normal_runs_stay_below_defect(baseline):
    for name in ("normal_box", "normal_cylinder", "normal_bracket"):
        run = baseline(name)
        assert run.meta.induction.method == "none"
        assert run.meta.defect_onset_s is None
        assert run.frame.true_defect.max() == 0
        assert run.frame.true_deficit.rolling(150).mean().max() < 0.05
        assert run.meta.outcome == "completed"


def test_normal_variation_defeats_a_fixed_force_threshold(baseline):
    """A fixed force threshold low enough to give a 5-minute warning on the slow clog presets
    false-alarms on some fault-free prints (filament batch, temperature, speed changes)."""
    def smooth(f):
        return f.force_g.rolling(300, min_periods=300).mean()

    levels = []
    for name in ("slow_viscous", "slow_heat_creep", "slow_debris"):
        run = baseline(name)
        f = run.frame
        i = (f.t - (run.meta.defect_onset_s - 300)).abs().idxmin()
        levels.append(smooth(f)[i])
    thr = min(levels)
    normals = [generate_run(random_normal(1, i)) for i in range(6)]
    alarms = [smooth(r.frame).max() > thr for r in normals]
    assert any(alarms)
    assert all(r.meta.defect_onset_s is None for r in normals)


@pytest.mark.parametrize("name", ["slow_viscous", "slow_heat_creep", "slow_debris"])
def test_slow_clog_defect_window_and_early_drift(baseline, name):
    run = baseline(name)
    onset = PRESETS[name].onset_s
    d = run.meta.defect_onset_s
    assert d is not None
    assert 8 * 60 <= d - onset <= 20 * 60
    # force drifts up measurably at least 5 minutes before the defect ...
    f = run.frame
    ex = f.volumetric_flow_mm3_s > 0.5
    pre = f[ex & (f.t >= onset - 300) & (f.t < onset)].force_g.mean()
    early = f[ex & (f.t >= d - 360) & (f.t < d - 300)].force_g.mean()
    assert early > 1.15 * pre
    # ... but the restriction there is still mild (warning is possible, not trivial)
    assert f[(f.t >= onset) & (f.t < onset + 120)].force_g.mean() < 1.6 * pre
    assert run.frame.loc[run.frame.t < onset, "true_defect"].max() == 0


def test_fast_clog_defects_quickly(baseline):
    run = baseline("fast_viscous")
    assert run.meta.defect_onset_s - PRESETS["fast_viscous"].onset_s < 8 * 60


def test_slowing_and_heating_recover_viscous(baseline):
    base = baseline("slow_viscous")
    ta = PRESETS["slow_viscous"].onset_s + 360
    a, b = ta + 120, ta + 420
    sc = preset("slow_viscous")
    slow = generate_run(sc, controller=once_at(ta, [SetSpeedFactor(percent=55)]))
    hot = generate_run(sc, controller=once_at(ta, [SetNozzleTemp(target_c=220)]))
    for run in (slow, hot):
        assert window(run, "true_restriction", a, b) < 0.7 * window(base, "true_restriction", a, b)
        assert window(run, "force_g", a, b) < 0.6 * window(base, "force_g", a, b)
        assert run.meta.defect_onset_s is None
        assert run.meta.purpose == "recovery_tuning"
    assert slow.meta.actions[0].kind == "set_speed_factor"
    assert slow.frame.speed_factor.iloc[-1] == pytest.approx(0.55)
    assert hot.frame.extruder_target_c.iloc[-1] == pytest.approx(220.0)
    assert slow.frame.t.iloc[-1] > base.frame.t.iloc[-1]  # slower print takes longer


def test_heat_creep_worsens_with_heat_but_recovers_by_slowing(baseline):
    base = baseline("slow_heat_creep")
    ta = PRESETS["slow_heat_creep"].onset_s + 360
    a, b = ta + 120, ta + 420
    hot = generate_run(preset("slow_heat_creep"),
                       controller=once_at(ta, [SetNozzleTemp(target_c=220)]))
    slow = generate_run(preset("slow_heat_creep"),
                        controller=once_at(ta, [SetSpeedFactor(percent=55)]))
    r0 = window(base, "true_restriction", a, b)
    assert window(hot, "true_restriction", a, b) > 1.3 * r0
    assert window(hot, "force_g", a, b) > window(base, "force_g", a, b)
    assert hot.meta.defect_onset_s < base.meta.defect_onset_s
    assert window(slow, "true_restriction", a, b) < 0.7 * r0


def test_debris_does_not_recover(baseline):
    base = baseline("slow_debris")
    ta = PRESETS["slow_debris"].onset_s + 360
    a, b = ta + 120, ta + 420
    both = generate_run(preset("slow_debris"), controller=once_at(
        ta, [SetSpeedFactor(percent=55), SetNozzleTemp(target_c=220), Notify(level="info",
                                                                            message="x")]))
    purged = generate_run(preset("slow_debris"), controller=once_at(ta, [RunPurge()]))
    r0 = window(base, "true_restriction", a, b)
    assert window(both, "true_restriction", a, b) == pytest.approx(r0, rel=0.05)
    assert both.frame.true_restriction.iloc[-1] > 0.5
    assert both.meta.defect_onset_s is not None  # only delayed, not prevented
    assert base.meta.induction.recoverable is False
    rp = window(purged, "true_restriction", a, b)
    assert 0.8 * r0 < rp < r0  # purge helps a little
    assert purged.meta.wasted_filament_g > 0


def test_pause_ends_run():
    run = generate_run(preset("slow_viscous"),
                       controller=once_at(120.0, [Pause(reason="test")]))
    assert run.meta.outcome == "paused"
    assert run.frame.t.iloc[-1] < 130
    assert run.meta.actions[-1].kind == "pause"


def test_temp_step_scenario_marks_manipulated_variables():
    sc = Scenario(name="ts", geometry="box", seed=2, clog_type="viscous", profile="linear",
                  onset_s=300.0, ramp_s=900.0, severity=0.2, induction_label="temp_step")
    events = sc.scheduled_events()
    assert [e[2] for e in events] == [205.0, 200.0, 195.0]
    assert sc.manipulated_variables() == ["extruder_target_c", "extruder_temp_c"]
    of = Scenario(clog_type="viscous", profile="linear", severity=0.3,
                  induction_label="over_flow")
    assert of.scheduled_events() == [(600.0, "speed_factor", 1.4)]
    assert "volumetric_flow_mm3_s" in of.manipulated_variables()
    with pytest.raises(ValueError):
        Scenario(clog_type="debris", profile="linear", induction_label="temp_step")
    with pytest.raises(ValueError):
        Scenario(clog_type="viscous")  # profile none with a clog


def test_dataset_mix_and_cli_roundtrip(tmp_path):
    kinds = {random_clog(1, i).clog_type for i in range(10)}
    assert kinds == {"viscous", "heat_creep", "debris"}
    assert {random_normal(1, i).geometry for i in range(3)} == set(GEOMETRIES)
    assert sim_main(["generate", "--n-normal", "1", "--n-clog", "1", "--seed", "3",
                     "--out", str(tmp_path)]) == 0
    dirs = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert len(dirs) == 2
    runs = [read_run(d) for d in dirs]
    assert {r.meta.is_clog_run for r in runs} == {True, False}
    assert all(r.meta.data_source == "simulated" for r in runs)
    with pytest.raises(FileExistsError):
        write_run(runs[0], tmp_path)
    assert sim_main(["list"]) == 0
