"""Fit the simulator's pressure model to measured (flow, temperature, force) rows.

Model (power-law melt in a round capillary, Arrhenius viscosity)::

    F = F0 + K (Q / Q0)^m exp(Ea/R (1/T - 1/T_ref)) (d0 / d)^n,   n = 3m + 1

With a single nozzle diameter ``n`` is not identifiable on its own, so it is inferred from the
flow exponent ``m`` through the power-law relation ``n = 3m + 1`` (Newtonian m = 1 -> n = 4).
If the rows carry a ``nozzle_d_mm`` column with at least two distinct diameters, ``n`` is
fitted directly instead. Intended for the TU Darmstadt hotend force data or the team's own
baseline prints; results are only as good as those rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from clogguard.sim.physics import R_GAS, PhysicsParams


@dataclass(frozen=True)
class PressureFit:
    n: float
    flow_exponent: float
    scale_g: float  # K at Q0, T_ref, d0
    friction_g: float
    activation_energy_j_mol: float
    r2: float
    n_rows: int
    n_fitted_directly: bool

    def to_params(self, base: PhysicsParams | None = None) -> PhysicsParams:
        b = base or PhysicsParams()
        return b.replace(pressure_n=self.n, flow_exponent=self.flow_exponent,
                         pressure_scale_g=self.scale_g, friction_g=self.friction_g,
                         activation_energy_j_mol=self.activation_energy_j_mol)


def fit_pressure_model(df: pd.DataFrame, flow_col: str = "volumetric_flow_mm3_s",
                       temp_col: str = "extruder_temp_c", force_col: str = "force_g",
                       d_col: str = "nozzle_d_mm", fit_activation: bool = True,
                       params: PhysicsParams | None = None, min_flow: float = 0.5) -> PressureFit:
    """Least-squares fit of the pressure model. Rows with flow below ``min_flow`` are ignored."""
    p = params or PhysicsParams()
    need = [flow_col, temp_col, force_col]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise KeyError(f"missing columns {missing}")
    rows = df[df[flow_col] > min_flow].dropna(subset=need)
    if len(rows) < 5:
        raise ValueError("need at least 5 rows with positive flow")
    q = rows[flow_col].to_numpy(float) / p.q0_mm3_s
    tk = rows[temp_col].to_numpy(float) + 273.15
    f = rows[force_col].to_numpy(float)
    d = rows[d_col].to_numpy(float) if d_col in rows.columns else np.full(len(rows), p.nozzle_d0_mm)
    direct = len(np.unique(np.round(d, 4))) >= 2
    inv_t = 1.0 / tk - 1.0 / (p.t_ref_c + 273.15)
    dr = p.nozzle_d0_mm / d

    def unpack(x: np.ndarray) -> tuple[float, float, float, float, float]:
        f0, log_k, m = x[0], x[1], x[2]
        ea = x[3] if fit_activation else p.activation_energy_j_mol / 1e4
        n = x[4] if direct else 3.0 * m + 1.0
        return f0, log_k, m, ea * 1e4, n

    def model(x: np.ndarray) -> np.ndarray:
        f0, log_k, m, ea, n = unpack(x)
        return f0 + np.exp(log_k) * q**m * np.exp(ea / R_GAS * inv_t) * dr**n

    x0 = [float(np.percentile(f, 1)) * 0.5, float(np.log(max(np.median(f), 1.0))), 0.7]
    lo, hi = [-1e4, -10.0, 0.05], [1e4, 20.0, 1.5]
    if fit_activation:
        x0.append(p.activation_energy_j_mol / 1e4)
        lo.append(1.0)
        hi.append(40.0)
    if direct:
        x0.append(3.0)
        lo.append(1.0)
        hi.append(6.0)
    sol = least_squares(lambda x: model(x) - f, np.asarray(x0), bounds=(lo, hi),
                        x_scale="jac")
    f0, log_k, m, ea, n = unpack(sol.x)
    resid = model(sol.x) - f
    ss_tot = float(np.sum((f - f.mean()) ** 2)) or 1.0
    return PressureFit(
        n=float(n),
        flow_exponent=float(m),
        scale_g=float(np.exp(log_k)),
        friction_g=float(f0),
        activation_energy_j_mol=float(ea),
        r2=1.0 - float(np.sum(resid**2)) / ss_tot,
        n_rows=len(rows),
        n_fitted_directly=direct,
    )
