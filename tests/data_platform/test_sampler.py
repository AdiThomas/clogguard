import math

import pytest

from clogguard.channels import DerivedSource, RawBuffer, Sampler, SourceContext, is_fast
from clogguard.core.channels import ChannelSpec
from clogguard.core.config import MaterialProfile

PLA = MaterialProfile(max_temp_c=220, nominal_temp_c=210)


def spec(name, source="moonraker", rate=4.0, **params):
    return ChannelSpec(name=name, unit="u", rate_hz=rate, source=source, params=params)


SPECS = [
    spec("temp_c", rate=4),
    spec("e_vel", rate=10),
    spec("force_g", source="serial", rate=80, port="/dev/x"),
    spec("accel_rms", source="serial", rate=10, port="/dev/y", raw_rate_hz=3200),
    spec("flow", source="derived", rate=10, fn="volumetric_flow", inputs=["e_vel"]),
]


def make(keep_raw=False):
    d = DerivedSource([SPECS[-1]], SourceContext(material=PLA))
    return Sampler(SPECS, derived=d, keep_raw=keep_raw)


def test_fast_classification():
    s = make()
    assert s.fast == {"force_g", "accel_rms"}
    assert is_fast(SPECS[2]) and not is_fast(SPECS[0])
    assert s.columns == ["temp_c", "e_vel", "force_g", "accel_rms", "flow"]


def test_zero_order_hold_for_slow():
    s = make()
    s.reset(origin=100.0)
    s.push("temp_c", 200.0, 100.02)
    r1 = s.tick(100.1)
    r2 = s.tick(100.2)
    assert r1["t"] == pytest.approx(0.1)
    assert r1["temp_c"] == r2["temp_c"] == 200.0
    s.push("temp_c", 201.0, 100.25)
    assert s.tick(100.3)["temp_c"] == 201.0
    assert math.isnan(r1["e_vel"]) and math.isnan(r1["flow"])


def test_mean_for_fast_within_tick():
    s = make()
    for i, v in enumerate([1.0, 2.0, 3.0, 6.0]):
        s.push("force_g", v, 0.01 + i * 0.02)  # 0.01 .. 0.07
    s.push("force_g", 100.0, 0.15)  # belongs to the next tick
    assert s.tick(0.1)["force_g"] == pytest.approx(3.0)
    assert s.tick(0.2)["force_g"] == pytest.approx(100.0)
    assert s.tick(0.3)["force_g"] == pytest.approx(100.0)  # no samples: hold last mean


def test_derived_and_none_values():
    s = make()
    s.push("e_vel", 2.0, 0.05)
    s.push("e_vel", None, 0.06)  # ignored
    s.push("e_vel", float("nan"), 0.07)  # ignored
    row = s.tick(0.1)
    assert row["e_vel"] == 2.0
    assert row["flow"] == pytest.approx(2.0 * PLA.filament_area_mm2)
    with pytest.raises(KeyError):
        s.push("nope", 1.0, 0.1)


def test_staleness_ages():
    s = make()
    s.push_many({"temp_c": 200.0, "e_vel": 1.0}, 10.0)
    s.push("force_g", 5.0, 12.0)
    s.push("e_vel", 1.0, 11.0)
    ages = s.ages(13.0)
    assert ages["temp_c"] == pytest.approx(3.0)
    assert ages["e_vel"] == pytest.approx(2.0)
    assert ages["force_g"] == pytest.approx(1.0)
    assert ages["accel_rms"] is None
    assert ages["flow"] == pytest.approx(2.0)  # age of its input
    assert sorted(s.stale(13.0, max_age_s=2.5)) == ["accel_rms", "temp_c"]
    # an out-of-order (older) sample does not make a channel look fresher
    s.push("temp_c", 199.0, 9.0)
    assert s.age("temp_c", 13.0) == pytest.approx(3.0)


def test_derived_age_none_when_input_unseen():
    s = make()
    assert s.age("flow", 1.0) is None


def test_raw_buffer():
    s = make(keep_raw=True)
    s.reset(origin=50.0)
    s.push("force_g", 1.0, 50.01)
    s.push("force_g", 2.0, 50.02)
    s.push("accel_rms", 0.5, 50.03)
    s.push("temp_c", 200.0, 50.03)  # slow channel: not raw
    assert s.raw is not None and len(s.raw) == 3
    frames = s.raw.drain()
    assert list(frames["force_g"]["t"]) == pytest.approx([0.01, 0.02])
    assert list(frames["force_g"]["value"]) == [1.0, 2.0]
    assert len(s.raw) == 0 and s.raw.drain() == {}
    s.push("force_g", 3.0, 51.0)
    s.reset(origin=60.0)  # new run discards pending raw samples and fast accumulators
    assert len(s.raw) == 0
    assert RawBuffer(["a"]).channels == ["a"]
