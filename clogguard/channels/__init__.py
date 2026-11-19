"""Channel-source plugins (keyed by ``SourceKind``) and the base-rate sampler."""

from clogguard.channels.base import (
    ChannelSource,
    ClockAligner,
    SampleSink,
    SourceContext,
    build_sources,
    register_source,
    registered_kinds,
    to_float,
)
from clogguard.channels.derived import DERIVED_FUNCTIONS, DerivedSource, register_derived
from clogguard.channels.moonraker_source import MoonrakerSource, merge_subscriptions
from clogguard.channels.replay_source import ReplaySource
from clogguard.channels.sampler import RawBuffer, Sampler, is_fast
from clogguard.channels.serial_source import SerialSource, pyserial_lines

__all__ = [
    "DERIVED_FUNCTIONS",
    "ChannelSource",
    "ClockAligner",
    "DerivedSource",
    "MoonrakerSource",
    "RawBuffer",
    "ReplaySource",
    "SampleSink",
    "Sampler",
    "SerialSource",
    "SourceContext",
    "build_sources",
    "is_fast",
    "merge_subscriptions",
    "pyserial_lines",
    "register_derived",
    "register_source",
    "registered_kinds",
    "to_float",
]
