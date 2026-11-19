"""Simulated print scenarios and Run generation.

``generate_run(scenario)`` runs one closed-loop simulated print and returns a core ``Run`` with
``data_source="simulated"``. Every run made here is simulated and must never be reported as
real-printer performance.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from clogguard.core.channels import ChannelConfig
from clogguard.core.commands import Command, Notify, Pause, RunPurge, SetNozzleTemp, SetSpeedFactor
from clogguard.core.schema import (
    ActionRecord,
    DefectRating,
    InductionSpec,
    Run,
    RunMetadata,
    ScheduleStep,
)
from clogguard.sim.physics import CLOG_PROFILES, CLOG_TYPES, RECOVERABLE, PhysicsParams
from clogguard.sim.printjob import GEOMETRIES
from clogguard.sim.simulator import COLUMNS, DT_S, Simulator

CHANNELS_SIM_YAML = Path(__file__).with_name("channels_sim.yaml")
SIM_TRUTH_RATER = "sim_truth"
INDUCTION_LABELS = ("none", "natural", "temp_step", "over_flow")
_FLOW_VARS = ["speed_factor", "e_velocity_mm_s", "volumetric_flow_mm3_s", "toolhead_velocity_mm_s"]
_TEMP_VARS = ["extruder_target_c", "extruder_temp_c"]

Controller = Callable[[float, pd.DataFrame], Sequence[Command]]


@dataclass(frozen=True)
class Scenario:
    """Everything needed to reproduce one simulated print.

    ``severity`` is the target fractional reduction of the effective nozzle diameter (0..1) that
    the clog profile reaches ``ramp_s`` seconds after ``onset_s``. For viscous and heat-creep
    clogs the actual restriction is ``severity`` scaled by the closed-loop growth factor (flow
    and temperature), so interventions change it. ``induction_label`` says how the clog is meant
    to have been induced: ``natural`` (no manipulated variable), ``temp_step`` (target lowered from
    ``onset_s`` in steps of ``temp_step_size_c`` every ``temp_step_interval_s`` down to
    ``nominal + temp_step_c``; temperature channels become manipulated variables) or
    ``over_flow`` (speed factor raised to ``over_flow_factor`` at onset; flow channels become
    manipulated variables).
    """

    name: str = "custom"
    geometry: str = "box"
    seed: int = 0
    clog_type: str = "none"
    onset_s: float = 600.0
    profile: str = "none"
    ramp_s: float = 1200.0
    severity: float = 0.0
    noise_level: float = 1.0
    induction_label: str = "none"
    nominal_temp_c: float = 210.0
    temp_step_c: float = -15.0
    temp_step_size_c: float = 5.0
    temp_step_interval_s: float = 240.0
    over_flow_factor: float = 1.4
    # Normal-run variation by the "operator": (t_s, setting, value), settings speed_factor,
    # extruder_target_c or purge.
    operator_events: tuple[tuple[float, str, float], ...] = ()
    purge_duration_s: float = 12.0
    purge_flow_mm3_s: float = 6.0
    max_duration_factor: float = 3.0
    purpose: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.geometry not in GEOMETRIES:
            raise ValueError(f"geometry must be one of {GEOMETRIES}")
        if self.clog_type not in CLOG_TYPES:
            raise ValueError(f"clog_type must be one of {CLOG_TYPES}")
        if self.profile not in CLOG_PROFILES:
            raise ValueError(f"profile must be one of {CLOG_PROFILES}")
        if self.induction_label not in INDUCTION_LABELS:
            raise ValueError(f"induction_label must be one of {INDUCTION_LABELS}")
        if self.induction_label in ("temp_step", "over_flow") and self.clog_type != "viscous":
            raise ValueError("temp_step and over_flow inductions produce viscous clogs")
        if (self.clog_type == "none") != (self.profile == "none"):
            raise ValueError("clog_type 'none' goes with profile 'none' and vice versa")

    @property
    def is_clog(self) -> bool:
        return self.clog_type != "none"

    def with_seed(self, seed: int) -> Scenario:
        return replace(self, seed=seed)

    def scheduled_events(self) -> list[tuple[float, str, float]]:
        ev = list(self.operator_events)
        if self.induction_label == "temp_step":
            n = max(1, round(abs(self.temp_step_c) / self.temp_step_size_c))
            for k in range(1, n + 1):
                drop = max(-abs(self.temp_step_c), -self.temp_step_size_c * k)
                ev.append((self.onset_s + (k - 1) * self.temp_step_interval_s,
                           "extruder_target_c", self.nominal_temp_c + drop))
        elif self.induction_label == "over_flow":
            ev.append((self.onset_s, "speed_factor", self.over_flow_factor))
        return ev

    def manipulated_variables(self) -> list[str]:
        if self.induction_label == "temp_step":
            return list(_TEMP_VARS)
        if self.induction_label == "over_flow":
            return list(_FLOW_VARS)
        return []


# Defaults tuned so that, without intervention, slow clogs reach the defect label roughly
# 8-20 min after onset and force/StallGuard/accel drift several minutes earlier.
PRESETS: dict[str, Scenario] = {
    "normal_box": Scenario(name="normal_box", geometry="box"),
    "normal_cylinder": Scenario(name="normal_cylinder", geometry="cylinder"),
    "normal_bracket": Scenario(name="normal_bracket", geometry="bracket"),
    "slow_viscous": Scenario(name="slow_viscous", geometry="box", clog_type="viscous",
                             onset_s=480.0, profile="linear", ramp_s=1200.0, severity=0.6,
                             induction_label="natural"),
    "slow_heat_creep": Scenario(name="slow_heat_creep", geometry="cylinder",
                                clog_type="heat_creep", onset_s=480.0, profile="exponential",
                                ramp_s=1000.0, severity=0.6, induction_label="natural"),
    "slow_debris": Scenario(name="slow_debris", geometry="bracket", clog_type="debris",
                            onset_s=480.0, profile="linear", ramp_s=1200.0, severity=0.6,
                            induction_label="natural"),
    "fast_viscous": Scenario(name="fast_viscous", geometry="box", clog_type="viscous",
                             onset_s=480.0, profile="linear", ramp_s=300.0, severity=0.6,
                             induction_label="natural"),
}


def preset(name: str, **overrides: Any) -> Scenario:
    """A named preset, optionally with fields replaced (e.g. ``seed=3``)."""
    try:
        base = PRESETS[name]
    except KeyError:
        raise KeyError(f"unknown preset {name!r}; choose from {sorted(PRESETS)}") from None
    return replace(base, **overrides) if overrides else base


def sim_channel_config() -> ChannelConfig:
    return ChannelConfig.load(CHANNELS_SIM_YAML)


def _apply(sim: Simulator, cmd: Command, actions: list[ActionRecord]) -> bool:
    """Apply a core Command to the simulator. Returns True if the run should stop."""
    t = round(sim.print_t, 3)
    stop = False
    if isinstance(cmd, SetSpeedFactor):
        sim.set_speed_factor(cmd.percent / 100.0)
        detail: dict[str, float | str | int | bool] = {"percent": cmd.percent}
    elif isinstance(cmd, SetNozzleTemp):
        sim.set_target(cmd.target_c)
        detail = {"target_c": cmd.target_c}
    elif isinstance(cmd, RunPurge):
        sim.purge()
        detail = {}
    elif isinstance(cmd, Pause):
        sim.pause()
        detail = {"reason": cmd.reason}
        stop = True
    elif isinstance(cmd, Notify):
        detail = {"level": cmd.level, "message": cmd.message}
    else:  # pragma: no cover - Command is a closed union
        raise TypeError(f"unknown command {cmd!r}")
    actions.append(ActionRecord(t_s=t, kind=cmd.kind, detail=detail))
    return stop


def _run_id(scenario: Scenario) -> str:
    return f"sim_{scenario.name}_{scenario.geometry}_s{scenario.seed}"


def generate_run(scenario: Scenario, controller: Controller | None = None,
                 controller_interval_s: float = 1.0, run_id: str | None = None,
                 params: PhysicsParams | None = None) -> Run:
    """Simulate one print. ``controller(t_s, frame_so_far)`` is called every
    ``controller_interval_s`` of print time and may return core Commands, which are applied
    before the next step. ``frame_so_far`` is a read-only view including label columns."""
    sim = Simulator(scenario, params)
    sim.start()
    actions: list[ActionRecord] = []
    every = max(1, round(controller_interval_s / DT_S))
    paused = False
    k = 0
    while not sim.done:
        sim.step()
        k += 1
        if controller is not None and k % every == 0:
            for cmd in controller(sim.print_t, sim.frame_view()):
                paused = _apply(sim, cmd, actions) or paused
            if paused:
                break
    frame = sim.frame()
    return Run(meta=_metadata(scenario, sim, actions, controller is not None, paused, run_id),
               frame=frame)


def _metadata(scenario: Scenario, sim: Simulator, actions: list[ActionRecord],
              controlled: bool, paused: bool, run_id: str | None) -> RunMetadata:
    if scenario.is_clog:
        sched = [ScheduleStep(t_s=scenario.onset_s, setting="clog_onset",
                              value=scenario.clog_type)]
        sched += [ScheduleStep(t_s=t, setting=s, value=v) for t, s, v in scenario.scheduled_events()
                  if s != "purge" and (t, s, v) not in scenario.operator_events]
        induction = InductionSpec(
            method="simulated",
            severity=(f"{scenario.clog_type} {scenario.profile} r={scenario.severity:.2f} "
                      f"ramp={scenario.ramp_s:.0f}s"),
            schedule=sched,
            recoverable=RECOVERABLE[scenario.clog_type],
        )
        purpose = "recovery_tuning" if controlled else "clog_no_intervention"
    else:
        induction = InductionSpec(method="none")
        purpose = "baseline"
    defect = sim.first_defect_s()
    if sim.state == "complete":
        outcome = "completed"
    elif paused or sim.state == "paused":
        outcome = "paused"
    else:
        outcome = "cancelled"
    notes = (f"SIMULATED. induction_label={scenario.induction_label}; "
             f"onset_s={scenario.onset_s}; noise_level={scenario.noise_level}; "
             f"operator_events={list(scenario.operator_events)}; params={scenario.params}")
    try:
        from clogguard import __version__ as version
    except ImportError:  # pragma: no cover
        version = ""
    return RunMetadata(
        run_id=run_id or _run_id(scenario),
        data_source="simulated",
        started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=scenario.seed),
        purpose=scenario.purpose or purpose,  # type: ignore[arg-type]
        operator="simulator",
        printer="clogguard-sim",
        part=scenario.geometry,
        gcode_file=f"{scenario.geometry}_s{scenario.seed}.gcode",
        material="PLA",
        nozzle_mm=sim.params.nozzle_d0_mm,
        induction=induction,
        manipulated_variables=scenario.manipulated_variables(),
        defect_ratings=[DefectRating(rater=SIM_TRUTH_RATER, t_s=defect)],
        actions=actions,
        outcome=outcome,  # type: ignore[arg-type]
        part_mass_g=round(sim.delivered_mass_g, 3),
        reference_mass_g=round(sim.job.mass_g, 3),
        wasted_filament_g=round(sim.purged_mass_g, 3),
        channels=[c for c in COLUMNS if c != "t"],
        software_version=str(version),
        notes=notes,
    )


# --- dataset --------------------------------------------------------------------------------

_CLOG_TYPE_CYCLE = ("viscous", "heat_creep", "debris", "viscous", "heat_creep", "debris",
                    "viscous", "viscous", "heat_creep", "debris")  # 40/30/30
_PROFILE_CYCLE = ("linear", "exponential", "linear", "exponential", "step",
                  "linear", "exponential", "linear", "exponential", "linear")  # 50/40/10


def random_normal(seed: int, index: int = 0) -> Scenario:
    """A fault-free scenario with operator and environment variation."""
    rng = np.random.default_rng([seed, 1, index])
    geometry = GEOMETRIES[index % len(GEOMETRIES)]
    events: list[tuple[float, str, float]] = []
    nominal = float(rng.choice([200.0, 205.0, 210.0, 215.0]))
    if rng.random() < 1 / 3:  # operator speed change
        events.append((float(rng.uniform(300, 1500)), "speed_factor",
                       round(float(rng.uniform(0.75, 1.15)), 2)))
    if rng.random() < 1 / 4:  # operator temperature tweak
        events.append((float(rng.uniform(300, 1500)), "extruder_target_c",
                       nominal + float(rng.choice([-5.0, 5.0]))))
    return Scenario(
        name=f"normal_{geometry}",
        geometry=geometry,
        seed=int(rng.integers(0, 2**31 - 1)),
        nominal_temp_c=nominal,
        noise_level=round(float(rng.uniform(0.8, 1.3)), 3),
        operator_events=tuple(sorted(events)),
    )


def random_clog(seed: int, index: int = 0) -> Scenario:
    """A clog scenario drawn from the documented mix (see README)."""
    rng = np.random.default_rng([seed, 2, index])
    clog_type = _CLOG_TYPE_CYCLE[index % len(_CLOG_TYPE_CYCLE)]
    profile = _PROFILE_CYCLE[(index * 3) % len(_PROFILE_CYCLE)]
    if profile == "step":  # sudden partial obstruction: a lodged particle
        clog_type = "debris"
    geometry = GEOMETRIES[int(rng.integers(0, len(GEOMETRIES)))]
    onset = float(rng.uniform(300.0, 900.0))
    if profile == "step":
        severity, ramp = float(rng.uniform(0.12, 0.3)), 1.0
    elif profile == "exponential":
        severity, ramp = float(rng.uniform(0.55, 0.65)), float(rng.uniform(800.0, 1300.0))
    else:
        severity, ramp = float(rng.uniform(0.55, 0.65)), float(rng.uniform(1000.0, 1600.0))
    label = "natural"
    if clog_type == "viscous" and rng.random() < 0.25:
        label = "temp_step"
    return Scenario(
        name=f"{clog_type}_{profile}",
        geometry=geometry,
        seed=int(rng.integers(0, 2**31 - 1)),
        clog_type=clog_type,
        onset_s=onset,
        profile=profile,
        ramp_s=ramp,
        severity=severity if label != "temp_step" else severity * 0.35,
        noise_level=round(float(rng.uniform(0.8, 1.3)), 3),
        induction_label=label,
        nominal_temp_c=float(rng.choice([205.0, 210.0, 215.0])),
    )


def generate_dataset(n_normal: int, n_clog: int, seed: int) -> list[Run]:
    """``n_normal`` fault-free runs (geometries round-robin) then ``n_clog`` clog runs."""
    runs: list[Run] = []
    for i in range(n_normal):
        sc = random_normal(seed, i)
        runs.append(generate_run(sc, run_id=f"sim{seed}_n{i:03d}_{sc.geometry}"))
    for i in range(n_clog):
        sc = random_clog(seed, i)
        runs.append(generate_run(sc, run_id=f"sim{seed}_c{i:03d}_{sc.name}_{sc.geometry}"))
    return runs
