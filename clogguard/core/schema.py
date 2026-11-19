"""Run records: the on-disk format every workstream reads and writes.

Layout of one run directory (``runs/<run_id>/``)::

    meta.json             RunMetadata, schema-versioned
    timeseries.parquet    wide table at BASE_RATE_HZ: column ``t`` (s since print start) + one
                          column per channel at or below the base rate
    raw/<channel>.parquet optional full-rate samples for fast channels (columns ``t``, ``value``)
    photos/<n>.jpg        timestamped layer photos (path + t_s listed in meta.json)
    audit.jsonl           service audit log for runs where the service was active

Timestamps: ``t`` is monotonic seconds from print start. ``meta.started_at`` is the one
wall-clock anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1
BASE_RATE_HZ = 10.0

META_FILE = "meta.json"
TIMESERIES_FILE = "timeseries.parquet"
RAW_DIR = "raw"
PHOTO_DIR = "photos"
AUDIT_FILE = "audit.jsonl"

# Context channels the Moonraker logger always records. Names are fixed so models and the
# simulator agree. Units are in the name suffix.
CONTEXT_CHANNELS: dict[str, str] = {
    "extruder_temp_c": "extruder.temperature",
    "extruder_target_c": "extruder.target",
    "extruder_power": "extruder.power (0..1 heater duty)",
    "e_velocity_mm_s": "motion_report.live_extruder_velocity (filament mm/s)",
    "volumetric_flow_mm3_s": "derived: e_velocity_mm_s x filament cross-section",
    "toolhead_velocity_mm_s": "motion_report.live_velocity",
    "speed_factor": "gcode_move.speed_factor",
    "extrude_factor": "gcode_move.extrude_factor",
    "fan_speed": "fan.speed (part cooling, 0..1)",
    "z_mm": "toolhead.position[2]",
    "layer": "print_stats.info.current_layer (may be null)",
}

# Candidate clog signals (role=signal). The set actually present depends on the chosen concept.
CANDIDATE_SIGNALS: dict[str, str] = {
    "extruder_power": "hotend heater duty; zero added hardware",
    "tmc_sg_result": "extruder TMC StallGuard load value from drv_status (if readable)",
    "force_g": "extrusion force from a load cell (load_cell.force_g or external MCU)",
    "accel_rms": "hotend accelerometer RMS per base tick",
    "accel_band_*": "hotend accelerometer band energies per base tick",
    "encoder_slip": "commanded minus measured filament advance (mm) per base tick",
}

DataSource = Literal["real", "simulated", "open_dataset"]
RunPurpose = Literal[
    "baseline",
    "induction_trial",
    "clog_no_intervention",
    "recovery_tuning",
    "validation_clean",
    "validation_clog",
    "reference_part",
    "other",
]
InductionMethod = Literal[
    "none", "temp_step", "over_flow", "small_nozzle", "fan_reduce", "contaminated", "simulated"
]
Outcome = Literal["completed", "paused", "cancelled", "failed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleStep(_Model):
    t_s: float = Field(ge=0)
    setting: str
    value: float | str


class InductionSpec(_Model):
    method: InductionMethod
    severity: str = ""  # e.g. "0.25mm" or "-20C"; free text, stated in the test plan
    schedule: list[ScheduleStep] = Field(default_factory=list)
    recoverable: bool | None = None  # by the test plan's definition; None = not applicable


class DefectRating(_Model):
    rater: str
    t_s: float | None  # None = rater saw no defect


class ActionRecord(_Model):
    t_s: float
    kind: str  # Command.kind
    detail: dict[str, float | str | int | bool] = Field(default_factory=dict)
    accepted: bool = True


class PhotoRecord(_Model):
    t_s: float
    path: str  # relative to the run directory


class RunMetadata(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    data_source: DataSource
    started_at: datetime
    purpose: RunPurpose = "other"
    operator: str = ""
    printer: str = ""
    part: str = ""
    gcode_file: str = ""
    spool: str = ""
    material: str = "PLA"
    nozzle_mm: float = 0.4
    induction: InductionSpec = Field(default_factory=lambda: InductionSpec(method="none"))
    # Channels the experimenter manipulated to induce the clog (e.g. extruder_target_c for
    # temp_step). Training and scoring must exclude these as model inputs for this run.
    manipulated_variables: list[str] = Field(default_factory=list)
    service_active: bool = False
    service_mode: Literal["off", "watch", "act"] = "off"
    alert_time_s: float | None = None
    defect_ratings: list[DefectRating] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    outcome: Outcome | None = None
    part_mass_g: float | None = None
    reference_mass_g: float | None = None
    wasted_filament_g: float | None = None
    photos: list[PhotoRecord] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    software_version: str = ""
    config_hash: str = ""
    source_citation: str = ""  # required for open_dataset runs
    notes: str = ""

    @model_validator(mode="after")
    def _checks(self) -> RunMetadata:
        if self.data_source == "open_dataset" and not self.source_citation:
            raise ValueError("open_dataset runs must carry source_citation")
        if self.induction.method == "none" and self.manipulated_variables:
            raise ValueError("manipulated_variables set but induction.method is 'none'")
        return self

    @property
    def defect_onset_s(self) -> float | None:
        """Visible defect onset: the earliest time any blind rater marked a defect."""
        times = [r.t_s for r in self.defect_ratings if r.t_s is not None]
        return min(times) if times else None

    @property
    def is_clog_run(self) -> bool:
        return self.induction.method != "none"


@dataclass(frozen=True)
class Run:
    """A run loaded in memory: metadata plus the base-rate time series."""

    meta: RunMetadata
    frame: pd.DataFrame  # column "t" + channels, sorted by t, base rate

    @property
    def run_id(self) -> str:
        return self.meta.run_id

    def model_inputs(self, candidates: list[str]) -> list[str]:
        """Columns a model may use for this run: present, and not manipulated."""
        banned = set(self.meta.manipulated_variables)
        return [c for c in candidates if c in self.frame.columns and c not in banned]
