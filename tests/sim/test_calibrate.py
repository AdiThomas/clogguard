from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from clogguard.sim.calibrate import fit_pressure_model
from clogguard.sim.physics import PhysicsParams, pressure_force_g


def _rows(p: PhysicsParams, n: int = 400, diameters=(0.4,), seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    q = rng.uniform(2, 16, n)
    t = rng.uniform(195, 225, n)
    d = rng.choice(diameters, n)
    f = [p.friction_g + pressure_force_g(qi, ti, di, p) for qi, ti, di in zip(q, t, d,
                                                                             strict=True)]
    f = np.asarray(f) + rng.normal(0, 10, n)
    return pd.DataFrame({"volumetric_flow_mm3_s": q, "extruder_temp_c": t, "force_g": f,
                         "nozzle_d_mm": d})


def test_recovers_n_from_flow_exponent():
    truth = PhysicsParams(pressure_n=2.5)
    fit = fit_pressure_model(_rows(truth).drop(columns="nozzle_d_mm"))
    assert not fit.n_fitted_directly
    assert fit.n == pytest.approx(2.5, abs=0.15)
    assert fit.scale_g == pytest.approx(truth.pressure_scale_g, rel=0.1)
    assert fit.activation_energy_j_mol == pytest.approx(truth.activation_energy_j_mol, rel=0.15)
    assert fit.r2 > 0.98
    assert fit.to_params().pressure_n == pytest.approx(fit.n)


def test_fits_n_directly_with_several_diameters():
    truth = PhysicsParams(pressure_n=3.4, flow_exponent=0.6)
    fit = fit_pressure_model(_rows(truth, diameters=(0.3, 0.4, 0.5)))
    assert fit.n_fitted_directly
    assert fit.n == pytest.approx(3.4, abs=0.15)
    assert fit.flow_exponent == pytest.approx(0.6, abs=0.05)


def test_rejects_bad_input():
    with pytest.raises(KeyError):
        fit_pressure_model(pd.DataFrame({"force_g": [1.0]}))
    with pytest.raises(ValueError):
        fit_pressure_model(_rows(PhysicsParams(), n=3))
