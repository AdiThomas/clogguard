"""Service configuration. Loaded from YAML and validated at start-up."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialProfile(_Cfg):
    """Per-filament limits. ``max_temp_c`` comes from the manufacturer's datasheet."""

    name: str = "PLA"
    max_temp_c: float = Field(gt=0, le=320)
    nominal_temp_c: float = Field(gt=0, le=320)
    filament_diameter_mm: float = Field(default=1.75, gt=0, le=3.5)
    datasheet: str = ""  # URL or reference for max_temp_c

    @model_validator(mode="after")
    def _nominal_below_max(self) -> MaterialProfile:
        if self.nominal_temp_c > self.max_temp_c:
            raise ValueError("nominal_temp_c exceeds max_temp_c")
        return self

    @property
    def filament_area_mm2(self) -> float:
        return 3.141592653589793 * (self.filament_diameter_mm / 2) ** 2


class LadderConfig(_Cfg):
    """Recovery ladder tuning. Values are placeholders until recovery-tuning runs set them."""

    speed_steps_percent: list[int] = Field(default_factory=lambda: [85, 70, 55])
    temp_step_c: float = Field(default=5.0, gt=0, le=15)
    max_temp_raise_c: float = Field(default=15.0, ge=0, le=40)
    purge_enabled: bool = False
    purge_macro: str = Field(default="CLOGGUARD_PURGE", pattern=r"^[A-Z][A-Z0-9_]{0,40}$")
    verify_window_s: float = Field(default=60.0, gt=0)
    min_action_interval_s: float = Field(default=60.0, gt=0)
    max_interventions_per_print: int = Field(default=6, ge=1, le=50)
    pause_if_unrecoverable: bool = True

    @model_validator(mode="after")
    def _steps_decreasing(self) -> LadderConfig:
        s = self.speed_steps_percent
        if any(not 10 <= p <= 100 for p in s) or s != sorted(s, reverse=True):
            raise ValueError("speed_steps_percent must be decreasing values in 10..100")
        return self


class DetectorConfig(_Cfg):
    model_path: str = "models/current.joblib"
    assess_interval_s: float = Field(default=1.0, gt=0, le=1.0)  # criterion: at least 1 Hz
    stale_after_s: float = Field(default=3.0, gt=0)
    watch_threshold: float = 0.4  # p_clog thresholds
    warn_threshold: float = 0.6
    act_threshold: float = 0.8
    warmup_s: float = Field(default=120.0, ge=0)  # no decisions during first layers / heat-up


class ServiceConfig(_Cfg):
    moonraker_url: str = "ws://127.0.0.1:7125/websocket"
    api_key_env: str = "MOONRAKER_API_KEY"  # name of env var, never the key itself
    mode: Literal["watch", "act"] = "watch"  # watch = assess and alert only, never act
    material: MaterialProfile
    ladder: LadderConfig = Field(default_factory=LadderConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    channels_file: str = "config/channels.yaml"
    runs_dir: str = "runs"
    command_ack_timeout_s: float = Field(default=2.0, gt=0, le=10)

    @property
    def temp_ceiling_c(self) -> float:
        """The highest nozzle target the service may ever request."""
        return min(
            self.material.max_temp_c,
            self.material.nominal_temp_c + self.ladder.max_temp_raise_c,
        )

    @classmethod
    def load(cls, path: str | Path) -> ServiceConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]
