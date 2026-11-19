"""Seeded randomised fuzz of the safety gate.

Thousands of random commands (valid ones, out-of-range ones, and objects built with
``model_construct`` to skip validation), random configs and random timings. Invariants:

* every RPC ever sent is one of the exact templates, with values inside the config limits;
* watch mode sends nothing;
* corrective commands are at least ``min_action_interval_s`` apart and at most
  ``max_interventions_per_print`` per print;
* nothing is sent for a rejected command.
"""

from __future__ import annotations

import math
import random
import re
from typing import Any

from clogguard.core.commands import Notify, Pause, RunPurge, SetNozzleTemp, SetSpeedFactor
from clogguard.core.config import ServiceConfig
from clogguard.safety import SafetyGate
from clogguard.safety.templates import GCODE_METHOD, PAUSE_METHOD, is_safe_macro
from clogguard.service.link import InMemoryLink
from tests.safety.helpers import FakeClock, RecordingNotifier, make_cfg

SEED = 20261119
N_CONFIGS = 60
N_CMDS_PER_CONFIG = 80

SPEED = re.compile(r"M220 S(\d+)")
TEMP = re.compile(r"SET_HEATER_TEMPERATURE HEATER=extruder TARGET=(\d+\.\d)")
WEIRD = [math.nan, math.inf, -math.inf, -1.0, 0.0, 1e9, -1e9, 319.9, 320.0, 999.0]


def random_config(rng: random.Random) -> ServiceConfig:
    nominal = round(rng.uniform(170, 260), rng.choice([0, 1, 2]))
    max_temp = round(min(320.0, nominal + rng.uniform(0, 40)), rng.choice([0, 1, 2]))
    max_temp = max(max_temp, nominal)
    steps = sorted(rng.sample(range(10, 100), rng.randint(0, 4)), reverse=True)
    return make_cfg(
        rng.choice(["act", "act", "watch"]),
        nominal=nominal,
        max_temp=max_temp,
        ack_timeout=0.005,
        speed_steps_percent=steps,
        temp_step_c=rng.choice([1.0, 2.5, 5.0, 7.3]),
        max_temp_raise_c=rng.uniform(0, 40),
        purge_enabled=rng.random() < 0.5,
        purge_macro=rng.choice(["CLOGGUARD_PURGE", "PURGE_2", "M112", "G28"]),
        min_action_interval_s=rng.uniform(0.5, 30),
        max_interventions_per_print=rng.randint(1, 8),
    )


def random_number(rng: random.Random, cfg: ServiceConfig) -> Any:
    r = rng.random()
    if r < 0.3:
        return rng.choice(WEIRD)
    if r < 0.6:
        return cfg.material.nominal_temp_c + rng.uniform(-20, 30)
    if r < 0.8:
        return round(cfg.material.nominal_temp_c + rng.choice([0, 2.5, 5, 7.5, 10, 15]), 1)
    return rng.choice([True, False, "220", None, [], {}])


def random_command(rng: random.Random, cfg: ServiceConfig) -> object:
    steps = cfg.ladder.speed_steps_percent or [100]
    r = rng.random()
    if r < 0.15:
        return SetSpeedFactor(percent=rng.choice([*steps, 100, rng.randint(10, 100)]))
    if r < 0.25:
        return SetSpeedFactor.model_construct(percent=rng.choice(
            [rng.randint(-500, 500), 1000, 0, math.nan, 85.5, "85", None, True]))
    if r < 0.4:
        target = cfg.material.nominal_temp_c + rng.uniform(-30, 40)
        return SetNozzleTemp(target_c=min(320.0, max(0.0, target)))
    if r < 0.55:
        return SetNozzleTemp.model_construct(target_c=random_number(rng, cfg))
    if r < 0.62:
        return RunPurge() if rng.random() < 0.7 else RunPurge.model_construct(macro="M112")
    if r < 0.7:
        return Pause(reason="fuzz")
    if r < 0.75:
        return Notify(level=rng.choice(["info", "warning", "critical"]), message="fuzz")
    if r < 0.85:
        kind = rng.choice(["raw_gcode", "gcode", "set_speed_factor", "set_nozzle_temp",
                           "run_purge", "pause", "", None])
        cls = rng.choice([SetSpeedFactor, SetNozzleTemp, RunPurge, Pause, Notify])
        return cls.model_construct(kind=kind, percent=rng.randint(-10, 200),
                                   target_c=random_number(rng, cfg), script="M112",
                                   reason="x" * rng.choice([1, 300]), level="warning",
                                   message="m")
    if r < 0.95:
        return {"kind": rng.choice(["set_speed_factor", "set_nozzle_temp", "gcode"]),
                "percent": random_number(rng, cfg), "target_c": random_number(rng, cfg),
                "script": "M112"}
    return rng.choice(["M112", 42, None, b"G28", ["pause"]])


def check_call(method: str, params: dict[str, Any], cfg: ServiceConfig) -> str:
    """Return the template name, asserting the call matches it exactly and within limits."""
    if method == PAUSE_METHOD:
        assert params == {}
        return "pause"
    assert method == GCODE_METHOD and list(params) == ["script"]
    script = params["script"]
    if m := SPEED.fullmatch(script):
        pct = int(m.group(1))
        assert pct == 100 or pct in cfg.ladder.speed_steps_percent, script
        return "speed"
    if m := TEMP.fullmatch(script):
        v = float(m.group(1))
        assert cfg.material.nominal_temp_c <= v <= cfg.temp_ceiling_c, (script, cfg)
        return "temp"
    assert cfg.ladder.purge_enabled and script == cfg.ladder.purge_macro, script
    assert is_safe_macro(script)
    return "purge"


async def test_gate_fuzz() -> None:
    rng = random.Random(SEED)
    total_sent = 0
    total_submitted = 0
    for _ in range(N_CONFIGS):
        cfg = random_config(rng)
        link = InMemoryLink()
        gate = SafetyGate(cfg, link, RecordingNotifier(), clock=FakeClock())
        now = 0.0
        accepted_printer = 0
        corrective_times: list[float] = []
        escalations = 0
        speed, temp = 100, cfg.material.nominal_temp_c
        for i in range(N_CMDS_PER_CONFIG):
            if rng.random() < 0.05:
                gate.reset_for_new_print()
                escalations, corrective_times = 0, []
                speed, temp = 100, cfg.material.nominal_temp_c
            link.hang = rng.random() < 0.03
            link.fail_with = ConnectionError("x") if rng.random() < 0.03 else None
            r = rng.random()
            if r < 0.7:
                now += rng.uniform(0, 20)
            elif r < 0.8:
                now -= rng.uniform(0, 5)  # clock going backwards
            t = math.nan if rng.random() < 0.02 else now
            fresh = rng.choice([True, True, True, False, None, 1])
            n_before = len(link.calls)
            res = await gate.submit(random_command(rng, cfg), now_s=t, decision_t_s=t,
                                    data_fresh=fresh)
            total_submitted += 1
            new_calls = link.calls[n_before:]
            if not res.accepted or res.command.kind == "notify":
                assert new_calls == [], (i, res)
                continue
            assert len(new_calls) == 1
            kind = check_call(*new_calls[0], cfg)
            accepted_printer += 1
            if kind == "pause":
                continue
            assert fresh is True and math.isfinite(t)
            if corrective_times:
                assert t - corrective_times[-1] >= cfg.ladder.min_action_interval_s
            corrective_times.append(t)
            script = new_calls[0][1]["script"]
            if kind == "speed":
                pct = int(SPEED.fullmatch(script).group(1))  # type: ignore[union-attr]
                escalations += pct < speed
                speed = pct
            elif kind == "temp":
                v = float(TEMP.fullmatch(script).group(1))  # type: ignore[union-attr]
                escalations += v > temp
                temp = v
            else:
                escalations += 1
            assert escalations <= cfg.ladder.max_interventions_per_print
        if cfg.mode == "watch":
            assert link.calls == []
        assert len(link.calls) == accepted_printer
        total_sent += len(link.calls)
    assert total_submitted == N_CONFIGS * N_CMDS_PER_CONFIG
    assert total_sent > 100  # the fuzz actually exercised the send path
