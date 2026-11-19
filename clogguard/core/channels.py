"""Channel declarations.

A channel is one named time series with a unit, a nominal sample rate and a source. Adding or
removing a sensor is a change to the channel config file, never to code.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHANNEL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"


class SourceKind(StrEnum):
    """Where a channel's samples come from. Each kind is implemented by a plugin in
    ``clogguard.channels``."""

    MOONRAKER = "moonraker"  # a field of a Klipper printer object, via Moonraker subscription
    SERIAL = "serial"  # a line-oriented stream from an external microcontroller over USB serial
    REPLAY = "replay"  # a column of a stored run (for offline replays and tests)
    SIM = "sim"  # the clog simulator
    DERIVED = "derived"  # computed from other channels (e.g. volumetric flow)


ChannelRole = Literal["signal", "context", "label"]


class ChannelSpec(BaseModel):
    """One channel.

    ``role``:
      * ``signal``  - a candidate clog signal (heater power, force, vibration ...)
      * ``context`` - process state that explains normal variation (flow, temperature, speed)
      * ``label``   - ground truth only ever used for evaluation (e.g. simulated true restriction).
        Label channels must never be model inputs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=CHANNEL_NAME_PATTERN)
    unit: str = Field(min_length=1, max_length=32)
    rate_hz: float = Field(gt=0, le=20_000)
    source: SourceKind
    role: ChannelRole = "signal"
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class ChannelConfig(BaseModel):
    """The full channel set for one printer setup."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    channels: list[ChannelSpec]

    @field_validator("channels")
    @classmethod
    def _unique_names(cls, v: list[ChannelSpec]) -> list[ChannelSpec]:
        names = [c.name for c in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate channel names: {dupes}")
        return v

    @model_validator(mode="after")
    def _derived_inputs_exist(self) -> ChannelConfig:
        names = {c.name for c in self.channels}
        for c in self.channels:
            if c.source is SourceKind.DERIVED:
                missing = [i for i in c.params.get("inputs", []) if i not in names]
                if missing:
                    raise ValueError(f"derived channel {c.name!r} needs missing inputs {missing}")
        return self

    def by_role(self, role: ChannelRole) -> list[ChannelSpec]:
        return [c for c in self.channels if c.role == role]

    def names(self, role: ChannelRole | None = None) -> list[str]:
        return [c.name for c in self.channels if role is None or c.role == role]

    def get(self, name: str) -> ChannelSpec:
        for c in self.channels:
            if c.name == name:
                return c
        raise KeyError(name)

    @classmethod
    def load(cls, path: str | Path) -> ChannelConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
        )
