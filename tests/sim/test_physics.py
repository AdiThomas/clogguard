from __future__ import annotations

import numpy as np
import pytest

from clogguard.sim.physics import (
    ClogSpec,
    NozzleModel,
    PhysicsParams,
    StepInputs,
    delivered_flow,
    pressure_force_g,
    profile_value,
    q_max,
    viscosity_rel,
)

P = PhysicsParams()


def test_viscosity_falls_with_temperature():
    assert viscosity_rel(P.t_ref_c, P) == pytest.approx(1.0)
    assert viscosity_rel(200, P) > 1.0 > viscosity_rel(220, P)
    assert q_max(220, P) > q_max(200, P)


def test_force_rises_and_flow_falls_monotonically_with_restriction():
    rs = np.linspace(0.0, 0.8, 30)
    out = [delivered_flow(10.0, 210.0, P.nozzle_d0_mm * (1 - r), P) for r in rs]
    q_del = np.array([o[0] for o in out])
    force = np.array([o[1] for o in out])
    assert np.all(np.diff(force) > 0)
    assert np.all(np.diff(q_del) <= 1e-12)
    assert q_del[0] == pytest.approx(10.0, rel=0.02)  # unrestricted: full delivery
    assert q_del[-1] < 5.0  # heavily restricted: large deficit
    assert force[-1] <= P.grip_force_g * 1.001  # force saturates at the extruder grip


def test_pressure_exponents():
    f1 = pressure_force_g(10.0, 210.0, 0.4, P)
    assert f1 == pytest.approx(P.pressure_scale_g)
    assert pressure_force_g(10.0, 210.0, 0.2, P) / f1 == pytest.approx(2**P.pressure_n)
    assert P.m == pytest.approx((P.pressure_n - 1) / 3)
    lin = P.replace(flow_exponent=1.0)
    assert pressure_force_g(20.0, 210.0, 0.4, lin) / pressure_force_g(10.0, 210.0, 0.4, lin) \
        == pytest.approx(2.0)


def test_slowing_and_heating_reduce_pressure_at_fixed_restriction():
    d = 0.4 * 0.7
    _, f_base, _ = delivered_flow(10.0, 210.0, d, P)
    _, f_slow, _ = delivered_flow(5.5, 210.0, d, P)
    _, f_hot, _ = delivered_flow(10.0, 220.0, d, P)
    assert f_slow < f_base and f_hot < f_base


def test_profiles():
    for prof in ("linear", "exponential", "step"):
        vals = [profile_value(prof, t, 600.0, 0.5, P) for t in np.linspace(-10, 700, 50)]
        assert vals[0] == 0.0 and vals[-1] == pytest.approx(0.5)
        assert np.all(np.diff(vals) >= -1e-12)
    assert profile_value("exponential", 300, 600, 0.5, P) < profile_value("linear", 300, 600, 0.5,
                                                                          P)
    with pytest.raises(ValueError):
        profile_value("bogus", 1, 1, 0.5, P)
    with pytest.raises(ValueError):
        ClogSpec(clog_type="bogus")


def _drive(model: NozzleModel, seconds: float, q: float = 10.0, target: float = 210.0):
    out = {}
    for _ in range(int(seconds / P.dt_s)):
        out = model.step(StepInputs(q_cmd=q, e_v_cmd=q / P.filament_area_mm2, toolhead_v=80.0,
                                    toolhead_acc=500.0, target_c=target, fan=1.0))
    return out


def test_defect_label_needs_sustained_deficit():
    # step debris clog large enough to starve the nozzle
    m = NozzleModel(P, ClogSpec("debris", "step", onset_s=10.0, severity=0.6), seed=1,
                    q_ref_mm3_s=10.0, nominal_temp_c=210.0)
    _drive(m, 10.0)
    assert not m.defect
    times = []
    for k in range(600):
        out = _drive(m, 0.1)
        if out["true_defect"]:
            times.append(k * 0.1)
            break
    assert times, "defect never appeared"
    # EMA rise plus sustain time: never instantaneous
    assert times[0] >= P.defect_sustain_s
    # a short deficit burst does not latch a defect
    m2 = NozzleModel(P, ClogSpec(), seed=2, q_ref_mm3_s=10.0, nominal_temp_c=210.0)
    _drive(m2, 5.0, q=30.0)  # 5 s over capacity
    _drive(m2, 60.0, q=8.0)
    assert not m2.defect


def test_heater_duty_tracks_delivered_flow():
    hi = NozzleModel(P, ClogSpec(), seed=3, q_ref_mm3_s=10.0, nominal_temp_c=210.0,
                     noise_level=0.0)
    lo = NozzleModel(P, ClogSpec(), seed=3, q_ref_mm3_s=10.0, nominal_temp_c=210.0,
                     noise_level=0.0)
    _drive(hi, 300.0, q=12.0)
    _drive(lo, 300.0, q=1.0)
    assert hi.duty > lo.duty + 0.05
    assert abs(hi.temp - 210.0) < 1.5


def test_params_replace_rejects_unknown():
    with pytest.raises(KeyError):
        P.replace(not_a_param=1)
