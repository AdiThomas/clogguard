from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from clogguard.core import (
    ChannelConfig,
    InductionSpec,
    MaterialProfile,
    Run,
    RunMetadata,
    ServiceConfig,
)
from clogguard.core.commands import ALLOWED_KINDS, parse_command
from clogguard.core.events import Assessment, AuditRecord

ROOT = Path(__file__).resolve().parents[2]


def test_example_configs_load():
    channels = ChannelConfig.load(ROOT / "config/channels.example.yaml")
    assert "extruder_power" in channels.names("signal")
    assert "volumetric_flow_mm3_s" in channels.names("context")
    svc = ServiceConfig.load(ROOT / "config/service.example.yaml")
    assert svc.mode == "watch"
    assert svc.temp_ceiling_c == 220


def test_duplicate_channel_names_rejected():
    spec = {"name": "a", "unit": "x", "rate_hz": 1, "source": "sim"}
    with pytest.raises(ValidationError):
        ChannelConfig.model_validate({"channels": [spec, spec]})


def test_derived_channel_needs_inputs():
    with pytest.raises(ValidationError):
        ChannelConfig.model_validate(
            {"channels": [{"name": "q", "unit": "x", "rate_hz": 1, "source": "derived",
                           "params": {"inputs": ["missing"]}}]}
        )


def test_temp_ceiling_is_min_of_limits():
    svc = ServiceConfig(material=MaterialProfile(max_temp_c=230, nominal_temp_c=210))
    assert svc.temp_ceiling_c == 210 + svc.ladder.max_temp_raise_c
    assert svc.temp_ceiling_c <= svc.material.max_temp_c


def test_nominal_above_max_rejected():
    with pytest.raises(ValidationError):
        MaterialProfile(max_temp_c=200, nominal_temp_c=210)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "gcode", "script": "M104 S300"},
        {"kind": "set_speed_factor", "percent": 150},
        {"kind": "set_nozzle_temp", "target_c": 400},
        {"kind": "run_purge", "macro": "SAVE_CONFIG"},
        {"kind": "pause", "reason": "x", "script": "FIRMWARE_RESTART"},
    ],
)
def test_commands_reject_anything_outside_the_allowlist(bad):
    with pytest.raises(ValidationError):
        parse_command(bad)


def test_five_command_kinds():
    assert {"set_speed_factor", "set_nozzle_temp", "run_purge", "pause", "notify"} == ALLOWED_KINDS


def test_open_dataset_needs_citation():
    with pytest.raises(ValidationError):
        RunMetadata(run_id="x", data_source="open_dataset", started_at=datetime.now(UTC))


def test_manipulated_variables_are_excluded_from_inputs():
    meta = RunMetadata(
        run_id="r1",
        data_source="simulated",
        started_at=datetime.now(UTC),
        induction=InductionSpec(method="temp_step"),
        manipulated_variables=["extruder_target_c"],
    )
    frame = pd.DataFrame({"t": [0.0], "extruder_power": [0.5], "extruder_target_c": [200.0]})
    run = Run(meta, frame)
    inputs = run.model_inputs(["extruder_power", "extruder_target_c", "force_g"])
    assert inputs == ["extruder_power"]


def test_defect_onset_is_earliest_rating():
    meta = RunMetadata(
        run_id="r2",
        data_source="real",
        started_at=datetime.now(UTC),
        defect_ratings=[{"rater": "a", "t_s": 900}, {"rater": "b", "t_s": 840}],
    )
    assert meta.defect_onset_s == 840


def test_audit_record_roundtrip():
    rec = AuditRecord(
        seq=0,
        run_id="r",
        event=Assessment(t_s=1.0, wall_time=datetime.now(UTC), state="normal", p_clog=0.1),
    )
    again = AuditRecord.model_validate_json(rec.model_dump_json())
    assert again == rec
