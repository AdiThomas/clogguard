import math
from pathlib import Path

import pandas as pd
import pytest

from clogguard.channels import (
    ChannelSource,
    ClockAligner,
    DerivedSource,
    MoonrakerSource,
    ReplaySource,
    SerialSource,
    SourceContext,
    build_sources,
    merge_subscriptions,
    pyserial_lines,
    registered_kinds,
    to_float,
)
from clogguard.core.channels import ChannelConfig, ChannelSpec, SourceKind
from clogguard.core.config import MaterialProfile

ROOT = Path(__file__).resolve().parents[2]
PLA = MaterialProfile(max_temp_c=220, nominal_temp_c=210)


def spec(name, source, rate=10.0, **params):
    return ChannelSpec(name=name, unit="u", rate_hz=rate, source=source, params=params)


STATUS = {
    "extruder": {"temperature": 209.8, "power": 0.42, "can_extrude": True},
    "toolhead": {"position": [10.0, 20.0, 1.2, 55.0]},
    "tmc2209 extruder": {"drv_status": {"sg_result": 87, "cs_actual": 16}},
    "print_stats": {"info": {"current_layer": 6}},
    "clogguard_sim": {"force_g": "1234.5"},
}


def test_moonraker_mapping_index_key():
    src = MoonrakerSource(
        [
            spec("extruder_temp_c", "moonraker", object="extruder", field="temperature"),
            spec("can_extrude", "moonraker", object="extruder", field="can_extrude"),
            spec("z_mm", "moonraker", object="toolhead", field="position", index=2),
            spec("sg", "moonraker", object="tmc2209 extruder", field="drv_status", key="sg_result"),
            spec("layer", "moonraker", object="print_stats", field="info", key="current_layer"),
            spec("force_g", "moonraker", object="clogguard_sim", field="force_g"),
            spec("missing", "moonraker", object="fan", field="speed"),
            spec("bad_index", "moonraker", object="toolhead", field="position", index=9),
        ]
    )
    vals = src.extract(STATUS)
    assert vals == {
        "extruder_temp_c": 209.8,
        "can_extrude": 1.0,
        "z_mm": 1.2,
        "sg": 87.0,
        "layer": 6.0,
        "force_g": 1234.5,
        "missing": None,
        "bad_index": None,
    }
    sub = src.subscription()
    assert sub["extruder"] == ["temperature", "can_extrude"]
    assert sub["tmc2209 extruder"] == ["drv_status"]
    # drv_status is null when the driver is disabled
    assert src.extract({"tmc2209 extruder": {"drv_status": None}})["sg"] is None


def test_moonraker_param_errors():
    with pytest.raises(ValueError, match=r"params\.object"):
        MoonrakerSource([spec("x", "moonraker", field="a")])
    with pytest.raises(ValueError, match="index"):
        MoonrakerSource([spec("x", "moonraker", object="a", field="b", index="2")])


def test_merge_subscriptions():
    merged = merge_subscriptions(
        {"a": ["x"], "b": None}, {"a": ["y", "x"], "b": ["z"]}, {"c": None}
    )
    assert merged == {"a": ["x", "y"], "b": None, "c": None}


def test_to_float():
    assert to_float(None) is None
    assert to_float(float("nan")) is None
    assert to_float("abc") is None
    assert to_float([1]) is None
    assert to_float(False) == 0.0
    assert to_float("2.5") == 2.5


def test_registry_builds_example_config():
    cfg = ChannelConfig.load(ROOT / "config/channels.example.yaml")
    sources = build_sources(cfg, SourceContext(material=PLA))
    assert set(sources) == {SourceKind.MOONRAKER, SourceKind.SERIAL, SourceKind.DERIVED}
    for s in sources.values():
        assert isinstance(s, ChannelSource)
    serial = sources[SourceKind.SERIAL]
    assert isinstance(serial, SerialSource)
    # force_g and encoder_slip_mm share /dev/ttyACM0
    assert sorted(serial.ports[("/dev/ttyACM0", 250000)]) == ["encoder_slip_mm", "force_g"]
    assert SourceKind.REPLAY in registered_kinds()


def test_registry_unregistered_kind():
    specs = [spec("true_restriction", "sim")]
    with pytest.raises(KeyError, match="no channel-source plugin"):
        build_sources(specs)
    assert build_sources(specs, skip_unregistered=True) == {}


def test_derived_volumetric_flow():
    d = DerivedSource(
        [spec("flow", "derived", fn="volumetric_flow", inputs=["e_vel"])],
        SourceContext(material=PLA),
    )
    out = d.compute({"e_vel": 2.0})
    assert out["flow"] == pytest.approx(2.0 * math.pi * 0.875**2)
    assert d.compute({"e_vel": None})["flow"] is None
    assert d.compute({})["flow"] is None
    assert d.inputs("flow") == ["e_vel"]
    with pytest.raises(KeyError):
        d.inputs("nope")


def test_derived_errors():
    with pytest.raises(ValueError, match="unknown fn"):
        DerivedSource([spec("f", "derived", fn="nope", inputs=["a"])], SourceContext(material=PLA))
    with pytest.raises(ValueError, match="MaterialProfile"):
        DerivedSource([spec("f", "derived", fn="volumetric_flow", inputs=["a"])])
    with pytest.raises(ValueError, match="inputs"):
        DerivedSource([spec("f", "derived", fn="volumetric_flow")], SourceContext(material=PLA))


def test_clock_aligner_lower_envelope():
    a = ClockAligner(window=4)
    assert a.offset is None
    # remote clock = host - 100, with variable transport latency
    assert a.to_host(0.0, 100.05) == pytest.approx(100.05)
    assert a.to_host(1.0, 101.01) == pytest.approx(101.01)
    assert a.to_host(2.0, 102.2) == pytest.approx(102.01)  # late line placed at true time
    assert a.offset == pytest.approx(100.01)


async def _lines(items):
    for item in items:
        yield item


async def test_serial_fake_stream():
    specs = [
        spec("force_g", "serial", port="/dev/fake", baud=115200, key="F"),
        spec("slip_mm", "serial", port="/dev/fake", baud=115200, key="slip_mm"),
        spec("accel_rms", "serial", rate=10, port="/dev/fake2", raw_rate_hz=4),
    ]
    lines = {
        "/dev/fake": [b'{"t_mcu": 5.0, "F": 101.5, "slip_mm": 0.01}\n', "garbage\n", "\n", "[1]"],
        "/dev/fake2": ['{"accel_rms": [1, 2, 3, 4]}'],
    }
    clock = iter([50.0, 50.1, 50.2, 50.3, 50.4]).__next__
    src = SerialSource(
        specs,
        SourceContext(line_reader_factory=lambda port, baud: _lines(lines[port]), clock=clock),
    )
    got = []
    await src.run(lambda n, v, t: got.append((n, v, t)))
    assert ("force_g", 101.5, 50.0) in got
    assert ("slip_mm", 0.01, 50.0) in got
    accel = [(v, t) for n, v, t in got if n == "accel_rms"]
    assert [v for v, _ in accel] == [1, 2, 3, 4]
    t_last = accel[-1][1]
    assert [round(t_last - t, 3) for _, t in accel] == [0.75, 0.5, 0.25, 0.0]
    assert src.bad_lines == 2  # "garbage" and "[1]"; blank lines are not errors


def test_serial_needs_port():
    with pytest.raises(ValueError, match="port"):
        SerialSource([spec("f", "serial")])


async def test_pyserial_missing_is_clear():
    gen = pyserial_lines("/dev/ttyACM0", 250000)
    with pytest.raises(RuntimeError, match="pip install pyserial"):
        await gen.__anext__()


def test_replay_source():
    frame = pd.DataFrame({"t": [0.0, 0.1, 0.2], "p": [0.1, float("nan"), 0.3], "q": [1, 2, 3]})
    src = ReplaySource(
        [spec("power", "replay", column="p"), spec("q", "replay")], SourceContext(run=frame)
    )
    assert src.values_at(-1) == {"power": None, "q": None}
    assert src.values_at(0.15) == {"power": None, "q": 2.0}
    assert src.values_at(5) == {"power": 0.3, "q": 3.0}
    samples = list(src.iter_samples())
    assert samples[0] == (0.0, {"power": 0.1, "q": 1.0})
    assert len(samples) == 3
    with pytest.raises(ValueError, match=r"SourceContext\.run"):
        ReplaySource([spec("q", "replay")])
    with pytest.raises(ValueError, match="lacks"):
        ReplaySource([spec("zz", "replay")], SourceContext(run=frame))
    with pytest.raises(ValueError, match="'t'"):
        ReplaySource([spec("q", "replay")], SourceContext(run=frame.drop(columns="t")))
