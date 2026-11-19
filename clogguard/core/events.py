"""Events emitted by the real-time service. One JSON object per line in ``audit.jsonl``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from clogguard.core.commands import Command

HealthState = Literal["normal", "watch", "warning", "clog", "unknown"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_s: float  # seconds since print start (monotonic)
    wall_time: datetime


class Assessment(_Event):
    """The detector's view of the nozzle at one tick (at least once per second)."""

    type: Literal["assessment"] = "assessment"
    state: HealthState
    health: float | None = None  # standardised residual; 0 = healthy, larger = worse
    p_clog: float | None = Field(default=None, ge=0, le=1)
    severity: float | None = Field(default=None, ge=0, le=1)  # 0 none .. 1 blocked
    minutes_to_defect: float | None = None
    minutes_to_defect_lo: float | None = None
    minutes_to_defect_hi: float | None = None
    recoverable: bool | None = None
    data_fresh: bool = True
    model_id: str = ""


class Decision(_Event):
    """What the ladder chose to do after an assessment. ``command`` None = hold."""

    type: Literal["decision"] = "decision"
    ladder_level: int = Field(ge=0)
    command: Command | None = None
    reason: str


class CommandResult(_Event):
    """Outcome of passing a command through the safety gate and, if accepted, to the printer."""

    type: Literal["command_result"] = "command_result"
    command: Command
    accepted: bool
    rejected_reason: str | None = None
    rpc_method: str | None = None
    sent_at_s: float | None = None
    acked_at_s: float | None = None
    decision_t_s: float | None = None


class Recovery(_Event):
    type: Literal["recovery"] = "recovery"
    verified: bool
    ladder_level: int
    detail: str = ""


class Lifecycle(_Event):
    type: Literal["lifecycle"] = "lifecycle"
    what: Literal[
        "service_start",
        "service_stop",
        "print_start",
        "print_end",
        "data_stale",
        "data_restored",
        "printer_disconnected",
        "printer_ready",
        "intervention_cap_reached",
    ]
    detail: str = ""


AuditEvent = Annotated[
    Assessment | Decision | CommandResult | Recovery | Lifecycle, Field(discriminator="type")
]


class AuditRecord(BaseModel):
    """Envelope written as one JSON line."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    run_id: str
    event: AuditEvent
