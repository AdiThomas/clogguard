"""The complete set of commands ClogGuard can ever ask for.

There are exactly five. No type here carries free-form G-code. Field bounds are a first line of
defence; ``clogguard.safety`` is the gate that validates every command against the live config
and is the only code allowed to translate a command into a Moonraker call.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Cmd(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SetSpeedFactor(_Cmd):
    """Scale print speed. Only reductions (or a return to 100%) are representable."""

    kind: Literal["set_speed_factor"] = "set_speed_factor"
    percent: int = Field(ge=10, le=100)


class SetNozzleTemp(_Cmd):
    """Set the extruder heater target. The safety gate rejects targets above the material
    limit; it never silently lowers them."""

    kind: Literal["set_nozzle_temp"] = "set_nozzle_temp"
    target_c: float = Field(ge=0, le=320)


class RunPurge(_Cmd):
    """Run the single purge macro named in the service config."""

    kind: Literal["run_purge"] = "run_purge"


class Pause(_Cmd):
    kind: Literal["pause"] = "pause"
    reason: str = Field(max_length=200)


class Notify(_Cmd):
    """Tell the user. Goes to the notifier (dashboard, phone); never to the printer."""

    kind: Literal["notify"] = "notify"
    level: Literal["info", "warning", "critical"]
    message: str = Field(max_length=500)


Command = Annotated[
    SetSpeedFactor | SetNozzleTemp | RunPurge | Pause | Notify,
    Field(discriminator="kind"),
]

COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)
ALLOWED_KINDS: frozenset[str] = frozenset(
    {"set_speed_factor", "set_nozzle_temp", "run_purge", "pause", "notify"}
)
PRINTER_KINDS: frozenset[str] = ALLOWED_KINDS - {"notify"}


def parse_command(data: object) -> Command:
    """Parse untrusted data into a Command. Raises pydantic.ValidationError otherwise."""
    return COMMAND_ADAPTER.validate_python(data)
