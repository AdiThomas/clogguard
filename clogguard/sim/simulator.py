"""Closed-loop print simulator: a print job driven through the nozzle model at 10 Hz.

Used both offline (``scenarios.generate_run``) and by the mock Moonraker server. Commands
(speed factor, target temperature, purge, pause/resume/cancel) take effect on the next step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from clogguard.sim.physics import (
    ClogSpec,
    NozzleModel,
    PhysicsParams,
    StepInputs,
)
from clogguard.sim.printjob import FILAMENT_AREA_MM2, PLA_DENSITY_G_MM3, PrintJob, generate_job

if TYPE_CHECKING:
    from clogguard.sim.scenarios import Scenario

DT_S = 0.1

CONTEXT_COLUMNS = (
    "extruder_temp_c",
    "extruder_target_c",
    "e_velocity_mm_s",
    "volumetric_flow_mm3_s",
    "toolhead_velocity_mm_s",
    "speed_factor",
    "extrude_factor",
    "fan_speed",
    "z_mm",
    "layer",
)
SIGNAL_COLUMNS = ("extruder_power", "force_g", "tmc_sg_result", "accel_rms", "encoder_slip_mm")
LABEL_COLUMNS = ("true_restriction", "true_deficit", "true_defect")
COLUMNS = ("t", *CONTEXT_COLUMNS, *SIGNAL_COLUMNS, *LABEL_COLUMNS)
_COL = {c: i for i, c in enumerate(COLUMNS)}

PrintState = str  # "standby" | "printing" | "paused" | "complete" | "cancelled"


class Simulator:
    """One simulated print. ``step()`` advances 0.1 s of simulated time."""

    def __init__(self, scenario: Scenario, params: PhysicsParams | None = None) -> None:
        self.scenario = scenario
        base = params or PhysicsParams()
        self.params = base.replace(**scenario.params) if scenario.params else base
        self.job: PrintJob = generate_job(scenario.geometry, scenario.seed)
        q_ref = self.job.filament_mm / self.job.duration_s * FILAMENT_AREA_MM2
        self.model = NozzleModel(
            self.params,
            ClogSpec(scenario.clog_type, scenario.profile, scenario.onset_s, scenario.ramp_s,
                     scenario.severity),
            seed=scenario.seed * 7919 + 17,
            q_ref_mm3_s=q_ref,
            nominal_temp_c=scenario.nominal_temp_c,
            noise_level=scenario.noise_level,
        )
        self.state: PrintState = "standby"
        self.print_t = 0.0
        self.tau = 0.0  # job time
        self.speed_factor = 1.0
        self.extrude_factor = 1.0
        self.target_c = scenario.nominal_temp_c
        self.fan_override: float | None = None
        self.purge_remaining_s = 0.0
        self.purge_count = 0
        self._purge_pending = False
        self.e_position_mm = 0.0
        self.delivered_mm3 = 0.0
        self.purged_mm3 = 0.0
        self.max_print_s = scenario.max_duration_factor * self.job.duration_s + 600.0
        self._events = sorted(scenario.scheduled_events(), key=lambda e: e[0])
        self._buf = np.zeros((4096, len(COLUMNS)))
        self._n = 0
        self.last: dict[str, float] = self._idle_row()

    # -- controls ---------------------------------------------------------------------------
    def start(self) -> None:
        if self.state == "standby":
            self.state = "printing"

    def set_speed_factor(self, factor: float) -> None:
        self.speed_factor = min(max(float(factor), 0.1), 2.0)

    def set_target(self, target_c: float) -> None:
        self.target_c = max(float(target_c), 0.0)

    def purge(self) -> None:
        """Park-and-purge: freezes the job for ``purge_duration_s`` while extruding."""
        if self.state in ("printing", "paused"):
            self.purge_remaining_s = self.scenario.purge_duration_s
            self.purge_count += 1
            self._purge_pending = True

    def pause(self) -> None:
        if self.state == "printing":
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "printing"

    def cancel(self) -> None:
        if self.state in ("printing", "paused", "standby"):
            self.state = "cancelled"
            self.target_c = 0.0
            self.fan_override = 0.0

    @property
    def done(self) -> bool:
        return self.state in ("complete", "cancelled")

    @property
    def progress(self) -> float:
        return min(self.tau / self.job.duration_s, 1.0)

    # -- stepping ---------------------------------------------------------------------------
    def _apply_events(self) -> None:
        while self._events and self._events[0][0] <= self.print_t + 1e-9:
            _, setting, value = self._events.pop(0)
            if setting == "speed_factor":
                self.set_speed_factor(float(value))
            elif setting == "extruder_target_c":
                self.set_target(float(value))
            elif setting == "purge":
                self.purge()

    def step(self) -> dict[str, float]:
        if self.state == "standby":
            return self.last
        recording = self.state in ("printing", "paused")
        if recording:
            self.print_t += DT_S
            self._apply_events()
        purge = self._purge_pending
        self._purge_pending = False
        job = self.job
        i = job.index(self.tau)
        fan = float(job.fan[i]) if self.fan_override is None else self.fan_override
        th_v = th_a = e_v = e_p = 0.0
        if self.state == "printing":
            if self.purge_remaining_s > 0.0:
                self.purge_remaining_s -= DT_S
                e_v = e_p = self.scenario.purge_flow_mm3_s / FILAMENT_AREA_MM2
                self.purged_mm3 += self.scenario.purge_flow_mm3_s * DT_S
            else:
                tau1 = self.tau + DT_S * self.speed_factor
                v_m, e_m, ep_m, a_m = job.means(self.tau, tau1)
                th_v = v_m * self.speed_factor
                th_a = a_m
                e_v = e_m * self.speed_factor * self.extrude_factor
                e_p = ep_m * self.speed_factor * self.extrude_factor
                self.tau = tau1
        q_cmd = max(e_p, 0.0) * FILAMENT_AREA_MM2
        out = self.model.step(StepInputs(q_cmd=q_cmd, e_v_cmd=e_v, toolhead_v=th_v,
                                         toolhead_acc=th_a, target_c=self.target_c, fan=fan,
                                         purge=purge))
        self.e_position_mm += e_v * DT_S
        if self.purge_remaining_s <= 0.0:
            self.delivered_mm3 += out["delivered_flow_mm3_s"] * DT_S
        row = {
            "t": round(self.print_t, 6),
            "extruder_temp_c": out["extruder_temp_c"],
            "extruder_target_c": self.target_c,
            "e_velocity_mm_s": e_v,
            "volumetric_flow_mm3_s": e_v * FILAMENT_AREA_MM2,
            "toolhead_velocity_mm_s": th_v,
            "speed_factor": self.speed_factor,
            "extrude_factor": self.extrude_factor,
            "fan_speed": fan,
            "z_mm": float(job.z[i]),
            "layer": float(job.layer[i] + 1),
            "extruder_power": out["extruder_power"],
            "force_g": out["force_g"],
            "tmc_sg_result": out["tmc_sg_result"],
            "accel_rms": out["accel_rms"],
            "encoder_slip_mm": out["encoder_slip_mm"],
            "true_restriction": out["true_restriction"],
            "true_deficit": out["true_deficit"],
            "true_defect": out["true_defect"],
        }
        self.last = row
        if recording:
            self._record(row)
            if self.state == "printing" and self.tau >= job.duration_s:
                self.state = "complete"
                self.target_c = 0.0
                self.fan_override = 0.0
            elif self.print_t >= self.max_print_s:
                self.cancel()
        return row

    def _idle_row(self) -> dict[str, float]:
        row = dict.fromkeys(COLUMNS, 0.0)
        row.update(extruder_temp_c=self.model.temp, extruder_target_c=self.target_c,
                   extruder_power=self.model.duty, speed_factor=1.0, extrude_factor=1.0,
                   tmc_sg_result=self.params.sg_free_low, z_mm=0.0, layer=0.0,
                   true_restriction=self.model.r)
        return row

    def _record(self, row: dict[str, float]) -> None:
        if self._n >= len(self._buf):
            self._buf = np.concatenate([self._buf, np.zeros_like(self._buf)])
        self._buf[self._n] = [row[c] for c in COLUMNS]
        self._n += 1

    def run_to_end(self) -> None:
        self.start()
        while not self.done:
            self.step()

    # -- views ------------------------------------------------------------------------------
    def frame_view(self) -> pd.DataFrame:
        """Zero-copy view of the rows recorded so far (do not mutate)."""
        return pd.DataFrame(self._buf[: self._n], columns=list(COLUMNS), copy=False)

    def frame(self) -> pd.DataFrame:
        """Copy of all recorded rows (column ``t`` = seconds since print start)."""
        return pd.DataFrame(self._buf[: self._n].copy(), columns=list(COLUMNS))

    def column(self, name: str) -> np.ndarray:
        return self._buf[: self._n, _COL[name]]

    @property
    def n_rows(self) -> int:
        return self._n

    @property
    def delivered_mass_g(self) -> float:
        return self.delivered_mm3 * PLA_DENSITY_G_MM3

    @property
    def purged_mass_g(self) -> float:
        return self.purged_mm3 * PLA_DENSITY_G_MM3

    def first_defect_s(self) -> float | None:
        d = self.column("true_defect")
        idx = np.flatnonzero(d > 0.5)
        return float(self.column("t")[idx[0]]) if idx.size else None

    def summary(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "print_t": self.print_t,
            "progress": self.progress,
            "speed_factor": self.speed_factor,
            "target_c": self.target_c,
            "restriction": self.model.r,
            "purges": self.purge_count,
        }
