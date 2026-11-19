"""Nozzle, hotend and extruder model for the clog simulator.

Stepped at the base rate (10 Hz, ``dt = 0.1 s``) with ``substeps`` internal steps for the thermal
loop. Closed loop: speed factor, target temperature and purges change what happens next.

The model is deliberately simple and every constant is a documented, tunable parameter (see
``PhysicsParams`` and ``clogguard/sim/README.md``). It is never reported as real performance.

Chain per step::

    commanded flow Q_cmd ─► melt capacity Q_max(T) ─► Q_cap
    restriction r(t) ─► d = d0 (1 - r) ─► F_demand = F_fric + K (Q_cap/Q0)^m mu(T)/mu0 L (d0/d)^n
    F_demand ─► extruder grip limit ─► F_actual, Q_del ─► deficit = 1 - Q_del / Q_cmd
    Q_del ─► melt heat ─► thermal model + PID ─► heater duty (extruder_power)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, fields
from typing import Literal

ClogType = Literal["none", "viscous", "heat_creep", "debris"]
ClogProfile = Literal["none", "linear", "exponential", "step"]
CLOG_TYPES: tuple[str, ...] = ("none", "viscous", "heat_creep", "debris")
CLOG_PROFILES: tuple[str, ...] = ("none", "linear", "exponential", "step")
RECOVERABLE: dict[str, bool | None] = {
    "none": None,
    "viscous": True,
    "heat_creep": True,
    "debris": False,
}

R_GAS = 8.314  # J/(mol K)


@dataclass(frozen=True)
class PhysicsParams:
    """Tunable model constants. Bases are documented in ``clogguard/sim/README.md``."""

    dt_s: float = 0.1
    substeps: int = 2
    # --- geometry --------------------------------------------------------------------------
    nozzle_d0_mm: float = 0.4
    filament_area_mm2: float = math.pi * 0.875**2
    melt_length_rel: float = 1.0  # L / L_nominal
    # --- pressure: F = K (Q/Q0)^m mu(T)/mu(T_ref) L (d0/d)^n -----------------------------------
    pressure_n: float = 3.0  # diameter exponent (Newtonian 4; power-law fluid 3m+1)
    flow_exponent: float | None = None  # m; None -> (n - 1) / 3 (power-law consistency)
    pressure_scale_g: float = 1600.0  # K: pressure force at Q0, T_ref, d0, in grams-force
    q0_mm3_s: float = 10.0
    t_ref_c: float = 210.0
    activation_energy_j_mol: float = 90_000.0  # PLA flow activation energy (Arrhenius)
    pressure_tau_s: float = 0.15  # melt compressibility / filament compliance lag
    # --- melt capacity ---------------------------------------------------------------------
    qmax_ref_mm3_s: float = 22.0  # at T_ref
    qmax_slope_per_c: float = 0.02  # fractional change of Q_max per degC
    qmax_sharpness: float = 8.0
    melt_dt_at_qmax_c: float = 15.0  # melt core colder than block at Q = Q_max
    # --- extruder --------------------------------------------------------------------------
    friction_g: float = 150.0
    friction_per_mm_s_g: float = 8.0
    grip_force_g: float = 6000.0  # push force at which the drive gear starts to slip
    grip_sharpness: float = 8.0
    # --- restriction dynamics --------------------------------------------------------------
    restriction_tau_rise_s: float = 20.0
    restriction_tau_fall_s: float = 60.0
    restriction_max: float = 0.9
    flow_ema_tau_s: float = 60.0
    clog_flow_alpha: float = 1.5  # viscous/heat-creep growth vs flow (Q_ema/Q_ref)^alpha
    viscous_temp_k_per_c: float = 0.06  # viscous growth factor exp(-k (T - T_nom))
    heat_creep_temp_k_per_c: float = 0.08  # heat-creep growth factor exp(+k (T - T_nom))
    clog_factor_max: float = 3.0
    purge_relief: dict[str, float] = field(
        default_factory=lambda: {"viscous": 0.4, "heat_creep": 0.15, "debris": 0.1, "none": 0.0}
    )
    purge_relief_tau_s: float = 180.0  # viscous / heat-creep relief fades (cause persists)
    exp_profile_k: float = 3.0
    step_rise_s: float = 5.0
    # --- normal variation ------------------------------------------------------------------
    bg_restriction_mean: float = 0.01
    bg_restriction_sd: float = 0.015
    bg_restriction_tau_s: float = 300.0
    viscosity_batch_sd: float = 0.12  # per-run filament batch factor (lognormal sd)
    viscosity_drift_sd: float = 0.04
    viscosity_drift_tau_s: float = 600.0
    # --- thermal ---------------------------------------------------------------------------
    heat_capacity_j_k: float = 12.0
    heater_w: float = 40.0
    loss_w_k: float = 0.085
    loss_w_k_run_sd: float = 0.06  # per-run spread (sock fit, airflow)
    fan_loss_w_k: float = 0.025  # part fan blowing on the block at speed 1.0
    ambient_c: float = 24.0
    ambient_run_sd_c: float = 2.0
    ambient_drift_sd_c: float = 1.0
    ambient_drift_tau_s: float = 900.0
    density_g_mm3: float = 1.24e-3
    cp_j_gk: float = 2.0  # effective, includes a little latent heat
    pid_kp: float = 22.0 / 255.0  # Klipper PID_CALIBRATE-like values, per degC
    pid_ki: float = 1.08 / 255.0
    pid_kd: float = 114.0 / 255.0
    temp_smooth_s: float = 1.0  # Klipper smooth_time
    temp_noise_c: float = 0.12
    power_noise: float = 0.01
    # --- sensors ---------------------------------------------------------------------------
    force_noise_g: float = 25.0
    force_zero_drift_sd_g: float = 60.0
    force_zero_drift_tau_s: float = 600.0
    sg_free_low: float = 120.0
    sg_free_high: float = 260.0
    sg_per_kg: float = 25.0
    sg_noise: float = 8.0
    sg_period_s: float = 1.0
    accel_base: float = 0.6
    accel_per_mm_s: float = 0.008
    accel_per_mm_s2: float = 3.5e-4
    accel_noise_rel: float = 0.08
    accel_restriction_k: float = 3.0
    accel_slip_k: float = 2.5
    encoder_resolution_mm: float = 0.005
    encoder_noise_mm: float = 0.003
    # --- defect label ----------------------------------------------------------------------
    defect_deficit_threshold: float = 0.15
    defect_ema_tau_s: float = 15.0
    defect_sustain_s: float = 20.0
    extruding_min_mm3_s: float = 0.5

    @property
    def m(self) -> float:
        return (self.pressure_n - 1.0) / 3.0 if self.flow_exponent is None else self.flow_exponent

    def replace(self, **kw: object) -> PhysicsParams:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        unknown = set(kw) - set(data)
        if unknown:
            raise KeyError(f"unknown physics parameters: {sorted(unknown)}")
        data.update(kw)
        return PhysicsParams(**data)  # type: ignore[arg-type]


# --- pure functions (also used by calibrate.py and tests) ------------------------------------


def viscosity_rel(temp_c: float, p: PhysicsParams) -> float:
    """Arrhenius viscosity relative to T_ref: mu(T)/mu(T_ref)."""
    tk = max(temp_c, 100.0) + 273.15
    tr = p.t_ref_c + 273.15
    return math.exp(p.activation_energy_j_mol / R_GAS * (1.0 / tk - 1.0 / tr))


def q_max(temp_c: float, p: PhysicsParams) -> float:
    """Hotend melt-capacity limit in mm^3/s (linear in temperature, floored)."""
    return max(1.0, p.qmax_ref_mm3_s * (1.0 + p.qmax_slope_per_c * (temp_c - p.t_ref_c)))


def pressure_force_g(q: float, temp_c: float, d_mm: float, p: PhysicsParams,
                     visc_mult: float = 1.0) -> float:
    """Force (g) needed to push melt at flow ``q`` through diameter ``d_mm`` (no friction)."""
    if q <= 0.0:
        return 0.0
    return (
        p.pressure_scale_g
        * (q / p.q0_mm3_s) ** p.m
        * viscosity_rel(temp_c, p)
        * visc_mult
        * p.melt_length_rel
        * (p.nozzle_d0_mm / max(d_mm, 1e-3)) ** p.pressure_n
    )


def profile_value(profile: str, t_since_onset: float, ramp_s: float, severity: float,
                  p: PhysicsParams) -> float:
    """Intrinsic clog progression s(t) in 0..severity."""
    if profile == "none" or t_since_onset < 0.0 or severity <= 0.0:
        return 0.0
    if profile == "step":
        return severity * min(1.0, t_since_onset / max(p.step_rise_s, 1e-6))
    u = min(1.0, t_since_onset / max(ramp_s, 1e-6))
    if profile == "linear":
        return severity * u
    if profile == "exponential":
        k = p.exp_profile_k
        return severity * (math.exp(k * u) - 1.0) / (math.exp(k) - 1.0)
    raise ValueError(f"unknown clog profile {profile!r}")


def delivered_flow(q_cmd: float, temp_c: float, d_mm: float, p: PhysicsParams,
                   visc_mult: float = 1.0, e_v: float | None = None
                   ) -> tuple[float, float, float]:
    """Steady-state (Q_delivered, F_actual_g, grip efficiency) for a commanded flow."""
    ev = q_cmd / p.filament_area_mm2 if e_v is None else e_v
    fric = p.friction_g + p.friction_per_mm_s_g * abs(ev)
    if q_cmd <= 0.0:
        return 0.0, (fric if ev > 0 else -fric if ev < 0 else 0.0), 1.0
    qm = q_max(temp_c, p)
    q_cap = q_cmd / (1.0 + (q_cmd / qm) ** p.qmax_sharpness) ** (1.0 / p.qmax_sharpness)
    t_melt = temp_c - p.melt_dt_at_qmax_c * min(q_cap / qm, 1.5) ** 2
    f_press = pressure_force_g(q_cap, t_melt, d_mm, p, visc_mult)
    f_demand = fric + f_press
    g = p.grip_sharpness
    f_actual = f_demand / (1.0 + (f_demand / p.grip_force_g) ** g) ** (1.0 / g)
    ratio = max(f_actual - fric, 0.0) / max(f_press, 1e-9)
    q_del = q_cap * min(ratio, 1.0) ** (1.0 / p.m)
    return q_del, f_actual, f_actual / f_demand


# --- stateful model -------------------------------------------------------------------------


@dataclass(frozen=True)
class ClogSpec:
    clog_type: str = "none"
    profile: str = "none"
    onset_s: float = 600.0
    ramp_s: float = 1200.0
    severity: float = 0.0

    def __post_init__(self) -> None:
        if self.clog_type not in CLOG_TYPES:
            raise ValueError(f"clog_type must be one of {CLOG_TYPES}")
        if self.profile not in CLOG_PROFILES:
            raise ValueError(f"profile must be one of {CLOG_PROFILES}")
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError("severity must be in 0..1")


@dataclass
class StepInputs:
    """Per-step inputs (tick means over the step where noted)."""

    q_cmd: float  # commanded volumetric flow, mm^3/s (tick mean, >= 0 part)
    e_v_cmd: float  # commanded filament velocity, mm/s (tick mean; negative = retract)
    toolhead_v: float  # mm/s (tick mean)
    toolhead_acc: float  # |mm/s^2| (tick mean)
    target_c: float
    fan: float  # 0..1
    purge: bool = False


SIGNAL_KEYS = ("force_g", "extruder_power", "tmc_sg_result", "accel_rms", "encoder_slip_mm")
LABEL_KEYS = ("true_restriction", "true_deficit", "true_defect")


def _ou(x: float, mean: float, sd: float, tau: float, dt: float, rng: random.Random) -> float:
    """Ornstein-Uhlenbeck step with stationary sd ``sd`` and time constant ``tau``."""
    a = math.exp(-dt / tau)
    return mean + (x - mean) * a + sd * math.sqrt(1.0 - a * a) * rng.gauss(0.0, 1.0)


class NozzleModel:
    """Stateful hotend + nozzle + extruder model. Call ``step`` once per base tick."""

    def __init__(self, params: PhysicsParams, clog: ClogSpec, seed: int, q_ref_mm3_s: float,
                 nominal_temp_c: float, noise_level: float = 1.0) -> None:
        self.p = params
        self.clog = clog
        self.rng = random.Random(seed)
        self.noise = noise_level
        self.q_ref = max(q_ref_mm3_s, 0.5)
        self.t_nom = nominal_temp_c
        r = self.rng
        # per-run variation
        self.visc_batch = math.exp(r.gauss(0.0, params.viscosity_batch_sd))
        self.loss_w_k = params.loss_w_k * math.exp(r.gauss(0.0, params.loss_w_k_run_sd))
        self.ambient_mean = params.ambient_c + r.gauss(0.0, params.ambient_run_sd_c)
        # state
        self.t = 0.0
        self.temp = nominal_temp_c  # hotend block, starts at temperature (pre-heated)
        self.temp_meas = nominal_temp_c
        self.pid_integ = 0.0
        self.pid_prev = nominal_temp_c
        self.pid_deriv = 0.0
        self.duty = 0.0
        self.ambient = self.ambient_mean
        self.visc_drift = 0.0
        self.r_bg = params.bg_restriction_mean
        self.r = self.r_bg
        self.q_ema = self.q_ref
        self.relief = 0.0
        self.debris_relief = 0.0
        self.force_state = 0.0
        self.force_zero = 0.0
        self.sg_hold = params.sg_free_high
        self.sg_next_t = 0.0
        self.defect_ema = 0.0
        self.defect_above_s = 0.0
        self.defect = False
        self.out: dict[str, float] = {}
        self._init_pid_integral()

    def _init_pid_integral(self) -> None:
        p = self.p
        idle = self.loss_w_k * (self.temp - self.ambient) / p.heater_w
        self.pid_integ = idle / p.pid_ki if p.pid_ki > 0 else 0.0
        self.duty = idle

    # -- clog --------------------------------------------------------------------------------
    def clog_factor(self) -> float:
        """Closed-loop growth factor for flow- and temperature-driven clogs."""
        p = self.p
        flow = (max(self.q_ema, 0.0) / self.q_ref) ** p.clog_flow_alpha
        dt_c = self.temp - self.t_nom
        if self.clog.clog_type == "viscous":
            f = flow * math.exp(-p.viscous_temp_k_per_c * dt_c)
        elif self.clog.clog_type == "heat_creep":
            f = flow * math.exp(p.heat_creep_temp_k_per_c * dt_c)
        else:
            f = 1.0
        return min(max(f, 0.0), p.clog_factor_max)

    def purge(self) -> None:
        relief = self.p.purge_relief.get(self.clog.clog_type, 0.0)
        if self.clog.clog_type == "debris":
            self.debris_relief = min(0.5, self.debris_relief + relief * (1.0 - self.debris_relief))
        else:
            self.relief = min(0.95, self.relief + relief * (1.0 - self.relief))

    def _restriction_target(self) -> float:
        c = self.clog
        s = profile_value(c.profile, self.t - c.onset_s, c.ramp_s, c.severity, self.p)
        if c.clog_type == "debris":
            clog_r = s * (1.0 - self.debris_relief)
        elif c.clog_type in ("viscous", "heat_creep"):
            clog_r = s * self.clog_factor() * (1.0 - self.relief)
        else:
            clog_r = 0.0
        return min(self.r_bg + clog_r, self.p.restriction_max)

    # -- step --------------------------------------------------------------------------------
    def step(self, inp: StepInputs) -> dict[str, float]:
        p, rng, nz = self.p, self.rng, self.noise
        dt = p.dt_s
        self.t += dt
        if inp.purge:
            self.purge()

        # slow drifts (normal variation)
        self.ambient = _ou(self.ambient, self.ambient_mean, p.ambient_drift_sd_c,
                           p.ambient_drift_tau_s, dt, rng)
        self.visc_drift = _ou(self.visc_drift, 0.0, p.viscosity_drift_sd,
                              p.viscosity_drift_tau_s, dt, rng)
        self.r_bg = max(0.0, _ou(self.r_bg, p.bg_restriction_mean, p.bg_restriction_sd,
                                 p.bg_restriction_tau_s, dt, rng))
        self.force_zero = _ou(self.force_zero, 0.0, p.force_zero_drift_sd_g * nz,
                              p.force_zero_drift_tau_s, dt, rng)
        self.relief *= math.exp(-dt / p.purge_relief_tau_s)

        # restriction
        q_cmd = max(inp.q_cmd, 0.0)
        self.q_ema += (q_cmd - self.q_ema) * (1.0 - math.exp(-dt / p.flow_ema_tau_s))
        r_t = self._restriction_target()
        tau = p.restriction_tau_rise_s if r_t > self.r else p.restriction_tau_fall_s
        self.r += (r_t - self.r) * (1.0 - math.exp(-dt / tau))
        d = p.nozzle_d0_mm * (1.0 - self.r)

        # flow and force
        visc_mult = self.visc_batch * math.exp(self.visc_drift)
        q_del, f_ss, grip_eff = delivered_flow(q_cmd, self.temp, d, p, visc_mult, inp.e_v_cmd)
        self.force_state += (f_ss - self.force_state) * (1.0 - math.exp(-dt / p.pressure_tau_s))
        extruding = q_cmd > p.extruding_min_mm3_s
        deficit = (1.0 - q_del / q_cmd) if extruding else 0.0

        # thermal loop with PID (substeps)
        qm = q_max(self.temp, p)
        t_melt = self.temp - p.melt_dt_at_qmax_c * min(q_del / qm, 1.5) ** 2
        p_melt = q_del * p.density_g_mm3 * p.cp_j_gk * max(t_melt - self.ambient, 0.0)
        h = dt / max(p.substeps, 1)
        duty_sum = 0.0
        for _ in range(max(p.substeps, 1)):
            meas = self.temp + rng.gauss(0.0, p.temp_noise_c * nz)
            a = 1.0 - math.exp(-h / p.temp_smooth_s)
            self.temp_meas += (meas - self.temp_meas) * a
            err = inp.target_c - self.temp_meas
            self.pid_deriv += ((self.temp_meas - self.pid_prev) / h - self.pid_deriv) * a
            self.pid_prev = self.temp_meas
            if inp.target_c <= 0.0:
                duty = 0.0
            else:
                integ = self.pid_integ + err * h
                integ = min(max(integ, 0.0), 1.0 / max(p.pid_ki, 1e-9))
                duty = p.pid_kp * err + p.pid_ki * integ - p.pid_kd * self.pid_deriv
                if 0.0 <= duty <= 1.0:
                    self.pid_integ = integ  # anti-windup: integrate only when unsaturated
                duty = min(max(duty, 0.0), 1.0)
            loss = (self.loss_w_k + p.fan_loss_w_k * inp.fan) * (self.temp - self.ambient)
            self.temp += (duty * p.heater_w - loss - p_melt) * h / p.heat_capacity_j_k
            duty_sum += duty
        self.duty = duty_sum / max(p.substeps, 1)

        # sensors
        force = self.force_state + self.force_zero + rng.gauss(0.0, p.force_noise_g * nz)
        power = min(max(self.duty + rng.gauss(0.0, p.power_noise * nz), 0.0), 1.0)
        if self.t >= self.sg_next_t:
            self.sg_next_t = self.t + p.sg_period_s
            sg_free = p.sg_free_low + (p.sg_free_high - p.sg_free_low) * min(
                abs(inp.e_v_cmd) / 3.0, 1.0)
            sg = sg_free - p.sg_per_kg * max(self.force_state, 0.0) / 1000.0
            sg += rng.gauss(0.0, p.sg_noise * nz)
            self.sg_hold = float(min(max(round(sg), 0), 510))
        accel = (p.accel_base + p.accel_per_mm_s * inp.toolhead_v
                 + p.accel_per_mm_s2 * inp.toolhead_acc)
        accel *= max(0.2, 1.0 + rng.gauss(0.0, p.accel_noise_rel * nz))
        restr = self.r - p.bg_restriction_mean
        if restr > 0.0 and extruding:
            accel += p.accel_restriction_k * restr * restr * (0.5 + q_cmd / self.q_ref)
        accel += p.accel_slip_k * (1.0 - grip_eff)
        slip = max(q_cmd - q_del, 0.0) / p.filament_area_mm2 * dt
        slip += rng.gauss(0.0, p.encoder_noise_mm * nz)
        res = p.encoder_resolution_mm
        slip = round(slip / res) * res

        # defect label: deficit EMA (extruding ticks only) above threshold for a sustained time
        if extruding:
            k = 1.0 - math.exp(-dt / p.defect_ema_tau_s)
            self.defect_ema += (deficit - self.defect_ema) * k
        if self.defect_ema > p.defect_deficit_threshold:
            self.defect_above_s += dt
        else:
            self.defect_above_s = 0.0
        if self.defect_above_s >= p.defect_sustain_s:
            self.defect = True

        self.out = {
            "force_g": force,
            "extruder_power": power,
            "tmc_sg_result": self.sg_hold,
            "accel_rms": accel,
            "encoder_slip_mm": slip,
            "extruder_temp_c": self.temp_meas,
            "true_restriction": self.r,
            "true_deficit": deficit,
            "true_defect": 1.0 if self.defect else 0.0,
            "delivered_flow_mm3_s": q_del,
        }
        return self.out
