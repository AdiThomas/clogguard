from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from clogguard.datasets import ufrgs
from clogguard.datasets.accel import BAND_COLUMNS, accel_tick_features
from clogguard.datasets.registry import REGISTRY, UFRGS_LABELS, get
from clogguard.datasets.writer import read_run, write_run

FS = 3200.0


def test_registry_complete() -> None:
    assert set(REGISTRY) == {"ufrgs_vibration", "tud_force", "agh_anomaly", "caxton"}
    for name, d in REGISTRY.items():
        assert d.name == name
        assert d.landing_url.startswith("https://")
        assert d.licence
        assert d.citation and "(20" in d.citation  # APA: author (year)
        assert d.expected_files
        assert d.notes
        assert all(f.sha256 is None or len(f.sha256) == 64 for f in d.files)
    assert REGISTRY["tud_force"].doi == "10.48328/tudatalib-1772"
    with pytest.raises(KeyError):
        get("nope")


@pytest.mark.parametrize(
    ("name", "ids", "label", "started"),
    [
        ("benchy_1_healthy_08_01_13_38_00.parquet", (1,), "healthy", (8, 1, 13, 38, 0)),
        ("benchy_4_temp_220_08_01_21_29_52.parquet", (4,), "temp_220", (8, 1, 21, 29, 52)),
        ("benchy_15_loose_head_08_03_03_56_32.parquet", (15,), "loose_head", (8, 3, 3, 56, 32)),
        ("benchy_21_nozzle_03_08_04_15_03_11.parquet", (21,), "nozzle_03", (8, 4, 15, 3, 11)),
        ("benchy_23_nozzle_02_08_04_23_59_56.parquet", (23,), "nozzle_02", (8, 4, 23, 59, 56)),
        ("benchy_11_12_temp_230_08_02_16_48_42.parquet", (11, 12), "temp_230", (8, 2, 16, 48, 42)),
    ],
)
def test_parse_filename(name: str, ids: tuple[int, ...], label: str, started: tuple) -> None:
    info = ufrgs.parse_filename(Path("x") / name)
    assert info.print_ids == ids
    assert info.label == label
    assert info.started_at == datetime(ufrgs.ASSUMED_YEAR, *started)


def test_parse_filename_rejects() -> None:
    with pytest.raises(ValueError):
        ufrgs.parse_filename("benchy_1_broken_08_01_13_38_00.parquet")
    assert set(UFRGS_LABELS) == {
        "healthy",
        "temp_220",
        "temp_230",
        "loose_head",
        "nozzle_03",
        "nozzle_02",
    }


def _write_raw(path: Path, t: np.ndarray, xyz: np.ndarray, time_col: str = "timestamp") -> None:
    df = pd.DataFrame({"acc_x": xyz[:, 0], "acc_y": xyz[:, 1], "acc_z": xyz[:, 2]})
    if time_col:
        df.insert(0, time_col, (t * 1e6).astype("int64"))  # microseconds
    df.to_parquet(path, index=False)


@pytest.mark.parametrize(("freq", "band"), [(20.0, 0), (100.0, 1), (400.0, 2), (1200.0, 3)])
def test_sine_lands_in_band(freq: float, band: int) -> None:
    t = np.arange(0, 2.0, 1 / FS)
    x = 0.5 * np.sin(2 * np.pi * freq * t)
    out = accel_tick_features(t, np.column_stack([x, np.zeros_like(x), np.ones_like(x)]))
    assert len(out) == 20
    ms = out["accel_rms"].to_numpy() ** 2
    np.testing.assert_allclose(ms, 0.125, rtol=1e-6)  # (0.5^2)/2
    energy = out[list(BAND_COLUMNS)].to_numpy()
    np.testing.assert_allclose(energy.sum(axis=1), ms, rtol=1e-9)  # Parseval
    assert np.all(energy[:, band] / ms > 0.999)


def test_ufrgs_loader_10hz(tmp_path: Path) -> None:
    t = np.arange(0, 3.0, 1 / FS)
    x = np.sin(2 * np.pi * 100 * t)
    z = 1.0 + 0.1 * np.sin(2 * np.pi * 1000 * t)
    p = tmp_path / "benchy_21_nozzle_03_08_04_15_03_11.parquet"
    _write_raw(p, t, np.column_stack([x, np.zeros_like(x), z]))
    (run,) = ufrgs.load_file(p)
    f = run.frame
    assert len(f) == 30
    np.testing.assert_allclose(f["t"].to_numpy(), np.arange(1, 31) / 10)
    assert list(f.columns) == ["t", "accel_rms", *BAND_COLUMNS]
    np.testing.assert_allclose(f["accel_band_50_200"], 0.5, rtol=1e-6)
    np.testing.assert_allclose(f["accel_band_800_1600"], 0.005, rtol=1e-6)
    m = run.meta
    assert m.data_source == "open_dataset"
    assert m.source_citation == REGISTRY["ufrgs_vibration"].citation
    assert m.induction.method == "small_nozzle" and m.induction.severity == "0.3mm"
    assert ufrgs.fault_label(m) == "nozzle_03"
    # round trip through the tiny writer
    back = read_run(write_run(run, tmp_path / "runs"))
    assert back.meta.channels == ["accel_rms", *BAND_COLUMNS]
    pd.testing.assert_frame_equal(back.frame, f)


def test_ufrgs_two_run_file_and_no_time_column(tmp_path: Path) -> None:
    t1 = np.arange(0, 1.0, 1 / FS)
    t = np.concatenate([t1, t1 + 60.0])
    xyz = np.column_stack([np.sin(2 * np.pi * 30 * t), np.zeros_like(t), np.ones_like(t)])
    p = tmp_path / "benchy_11_12_temp_230_08_02_16_48_42.parquet"
    _write_raw(p, t, xyz)
    runs = ufrgs.load_file(p)
    assert [r.run_id for r in runs] == ["ufrgs_benchy_11_temp_230", "ufrgs_benchy_12_temp_230"]
    assert all(len(r.frame) == 10 for r in runs)
    assert all(r.meta.induction.method == "none" for r in runs)
    assert "temp_230" in runs[0].meta.part and "fault=temp_230" in runs[0].meta.notes

    # no time column: uniform at the nominal rate, healthy label
    q = tmp_path / "benchy_1_healthy_08_01_13_38_00.parquet"
    _write_raw(q, t1, xyz[: t1.size], time_col="")
    (run,) = ufrgs.load_file(q)
    assert len(run.frame) == 10 and run.meta.nozzle_mm == 0.4
    assert len(ufrgs.load_dir(tmp_path)) == 3


def test_detect_columns_variants() -> None:
    df = pd.DataFrame({"Time (s)": [0.0], "AccX (g)": [0.0], "AccY (g)": [0.0], "AccZ (g)": [1.0]})
    assert ufrgs.detect_columns(df) == ("Time (s)", ("AccX (g)", "AccY (g)", "AccZ (g)"))
    df2 = pd.DataFrame({"index": [0], "a": [0.0], "b": [0.0], "c": [1.0]})
    assert ufrgs.detect_columns(df2)[1] == ("index", "a", "b")
    with pytest.raises(ValueError):
        ufrgs.detect_columns(pd.DataFrame({"a": [1.0]}))
