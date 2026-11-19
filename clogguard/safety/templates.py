"""The fixed RPC templates. This module is the only place in ClogGuard that spells out a printer
RPC method or a G-code line.

Each builder takes an already validated value and returns an ``RpcCall``. The gate
(``clogguard.safety.gate``) checks limits before it calls a builder; ``matches_template`` lets
tests and reviewers check any recorded call against the same templates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

GCODE_METHOD = "printer.gcode.script"
PAUSE_METHOD = "printer.print.pause"

SPEED_RE = re.compile(r"M220 S(100|[1-9][0-9]?)")
TEMP_RE = re.compile(r"SET_HEATER_TEMPERATURE HEATER=extruder TARGET=([0-9]{1,3}\.[0-9])")
MACRO_RE = re.compile(r"[A-Z][A-Z0-9_]{0,40}")  # same pattern as LadderConfig.purge_macro
# A letter followed only by digits is a raw G-code (G28, M84, M112 ...), not a macro name.
# LadderConfig's pattern allows those, so the gate refuses them as purge macros.
RAW_GCODE_RE = re.compile(r"[A-Z][0-9]+")


@dataclass(frozen=True)
class RpcCall:
    method: str
    params: dict[str, Any] = field(default_factory=dict)


def format_temp(target_c: float) -> str:
    """The exact text sent for a temperature target: one decimal place."""
    return f"{target_c:.1f}"


def speed_factor_rpc(percent: int) -> RpcCall:
    return RpcCall(GCODE_METHOD, {"script": f"M220 S{int(percent)}"})


def nozzle_temp_rpc(target_c: float) -> RpcCall:
    script = f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={format_temp(target_c)}"
    return RpcCall(GCODE_METHOD, {"script": script})


def is_safe_macro(name: str) -> bool:
    """A macro name the gate will send: matches MACRO_RE and is not a raw G-code word."""
    return bool(MACRO_RE.fullmatch(name)) and not RAW_GCODE_RE.fullmatch(name)


def purge_rpc(macro: str) -> RpcCall:
    if not is_safe_macro(macro):
        raise ValueError(f"invalid purge macro name {macro!r}")
    return RpcCall(GCODE_METHOD, {"script": macro})


def pause_rpc() -> RpcCall:
    return RpcCall(PAUSE_METHOD, {})


def matches_template(method: str, params: dict[str, Any], purge_macro: str) -> str | None:
    """Classify a recorded call. Returns ``"speed"``, ``"temp"``, ``"purge"``, ``"pause"`` or
    None when the call matches no template. Value limits are checked by the caller."""
    if method == PAUSE_METHOD:
        return "pause" if params == {} else None
    if method != GCODE_METHOD or set(params) != {"script"}:
        return None
    script = params["script"]
    if not isinstance(script, str):
        return None
    if SPEED_RE.fullmatch(script):
        return "speed"
    if TEMP_RE.fullmatch(script):
        return "temp"
    if script == purge_macro and is_safe_macro(script):
        return "purge"
    return None
