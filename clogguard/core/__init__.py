"""Shared interfaces. Every workstream builds against these; change them only through the lead."""

from clogguard.core.channels import ChannelConfig, ChannelSpec, SourceKind
from clogguard.core.commands import (
    Command,
    Notify,
    Pause,
    RunPurge,
    SetNozzleTemp,
    SetSpeedFactor,
)
from clogguard.core.config import DetectorConfig, LadderConfig, MaterialProfile, ServiceConfig
from clogguard.core.events import (
    Assessment,
    AuditRecord,
    CommandResult,
    Decision,
    Lifecycle,
    Recovery,
)
from clogguard.core.schema import (
    BASE_RATE_HZ,
    CONTEXT_CHANNELS,
    DataSource,
    DefectRating,
    InductionSpec,
    Run,
    RunMetadata,
)

__all__ = [
    "BASE_RATE_HZ",
    "CONTEXT_CHANNELS",
    "Assessment",
    "AuditRecord",
    "ChannelConfig",
    "ChannelSpec",
    "Command",
    "CommandResult",
    "DataSource",
    "Decision",
    "DefectRating",
    "DetectorConfig",
    "InductionSpec",
    "LadderConfig",
    "Lifecycle",
    "MaterialProfile",
    "Notify",
    "Pause",
    "Recovery",
    "Run",
    "RunMetadata",
    "RunPurge",
    "ServiceConfig",
    "SetNozzleTemp",
    "SetSpeedFactor",
    "SourceKind",
]
