from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from clogguard.datasets import agh, caxton, tud
from clogguard.datasets.registry import REGISTRY
from clogguard.datasets.tabular import read_table, sanitize, time_to_seconds


def test_tud_csv_newtons(tmp_path: Path) -> None:
    n = 200  # 2 s at 100 Hz
    df = pd.DataFrame(
        {
            "time_ms": np.arange(n) * 10.0,
            "Force [N]": np.full(n, 2.0),
            "Extrusion Multiplier": np.full(n, 0.8),
            "Nozzle Temp": np.full(n, 195.0),
            "comment": ["x"] * n,
        }
    )
    p = tmp_path / "em_080.csv"
    df.to_csv(p, sep=";", index=False)
    run = tud.load_file(p)
    f = run.frame
    assert len(f) == 20
    np.testing.assert_allclose(f["force_g"], 2.0 * 1000 / 9.80665)
    assert {"extrusion_multiplier", "nozzle_temp"} <= set(f.columns)
    assert "comment" not in f.columns
    m = run.meta
    assert m.data_source == "open_dataset" and m.induction.method == "none"
    assert m.source_citation == REGISTRY["tud_force"].citation
    assert "extrusion_multiplier" in m.notes and "nozzle_temperature" in m.notes
    assert run.run_id == "tud_em_080"


def test_tud_parquet_grams_no_time_needs_rate(tmp_path: Path) -> None:
    p = tmp_path / "speed.parquet"
    pd.DataFrame({"load_g": np.arange(80.0), "speed": 50.0}).to_parquet(p)
    with pytest.raises(ValueError, match="rate_hz"):
        tud.load_file(p)
    run = tud.load_file(p, rate_hz=80.0)
    assert len(run.frame) == 10
    assert run.frame["force_g"].iloc[0] == pytest.approx(np.arange(8.0).mean())
    assert len(tud.load_dir(tmp_path, rate_hz=80.0)) == 1


def test_tud_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="force"):
        tud.table_to_frame(pd.DataFrame({"t": [0.0, 0.1], "x": [1, 2]}))
    h5 = tmp_path / "a.h5"
    h5.write_bytes(b"\x89HDF")
    with pytest.raises(RuntimeError, match="h5py"):
        read_table(h5)
    with pytest.raises(ValueError, match="unsupported"):
        read_table(tmp_path / "a.xyz")
    assert tud.find_force_column(pd.DataFrame({"Kraft_kg": [1.0]}))[1] == 1000.0


def test_agh_csv(tmp_path: Path) -> None:
    fs = 800
    t = np.arange(0, 1.0, 1 / fs)
    df = pd.DataFrame(
        {
            "timestamp": t,
            "acc_x": np.sin(2 * np.pi * 100 * t),
            "acc_y": 0.0,
            "acc_z": 1.0,
            "power": 120.0,
            "hotend_temp": 210.0,
            "bed temperature": 60.0,
            "label": np.where(t > 0.5, "nozzle_clog", "normal"),
        }
    )
    d = tmp_path / "clog"
    d.mkdir()
    df.to_csv(d / "run1.csv", index=False)
    run = agh.load_file(d / "run1.csv")
    f = run.frame
    assert len(f) == 10
    assert {"accel_x", "accel_z", "power_w", "temp_hotend_temp_c", "anomaly_label"} <= set(f)
    assert "temp_bed_temperature_c" in f.columns
    np.testing.assert_allclose(f["accel_rms"], np.sqrt(0.5), rtol=1e-3)
    assert "accel_band_50_200" in f.columns
    assert f["anomaly_label"].tolist() == [0.0] * 5 + [1.0] * 5
    assert run.meta.source_citation == REGISTRY["agh_anomaly"].citation
    assert "clog/run1" in run.meta.part
    assert len(agh.load_dir(tmp_path)) == 1


def test_agh_low_rate_no_bands(tmp_path: Path) -> None:
    t = np.arange(0, 2.0, 1 / 100)
    df = pd.DataFrame({"time": t, "x": np.sin(t * 60), "y": 0.0, "z": 1.0})
    f = agh.table_to_frame(df)
    assert "accel_rms" in f.columns and "accel_band_0_50" not in f.columns
    with pytest.raises(ValueError):
        agh.table_to_frame(df.drop(columns="time"))
    assert len(agh.table_to_frame(df.drop(columns="time"), rate_hz=100)) == 20


def test_caxton_labels(tmp_path: Path) -> None:
    n = 30
    df = pd.DataFrame(
        {
            "img_path": [f"print0/image-{i}.jpg" for i in range(n)],
            "timestamp": pd.date_range("2022-01-01", periods=n, freq="s").astype(str),
            "flow_rate": np.linspace(30, 200, n),
            "feed_rate": 100,
            "z_offset": 0.0,
            "target_hotend": 205,
            "hotend": 204.5,
            "bed": 60,
            "nozzle_tip_x": 320,
            "nozzle_tip_y": 240,
            "unused": 1,
        }
    )
    p = tmp_path / "caxton_dataset_filtered.csv"
    df.to_csv(p, index=False)
    all_rows = caxton.load_labels(p, chunksize=7)
    assert len(all_rows) == n and "unused" not in all_rows.columns
    assert set(all_rows["flow_rate_class"]) == {"low", "good", "high"}
    low = caxton.load_labels(p, flow_classes=["low"], chunksize=7)
    assert (low["flow_rate"] < 90).all() and len(low) > 0
    assert len(caxton.load_labels(p, max_rows=5)) == 5
    with pytest.raises(ValueError):
        caxton.load_labels(p, flow_classes=["bad"])
    coded = df.assign(flow_rate_class=np.arange(n) % 3)
    coded.to_csv(p, index=False)
    assert set(caxton.load_labels(p)["flow_rate_class"]) == {"low", "good", "high"}
    assert caxton.find_label_csv(tmp_path) == p


def test_tabular_helpers() -> None:
    assert sanitize("Nozzle Temp [°C]") == "nozzle_temp_c"
    assert sanitize("1st") == "c_1st"
    s = pd.Series(np.arange(5) * 312_500)  # nanoseconds at 3200 Hz
    np.testing.assert_allclose(time_to_seconds(s, nominal_rate_hz=3200)[1], 1 / 3200)
    dt = pd.Series(pd.date_range("2025-01-01", periods=3, freq="100ms"))
    np.testing.assert_allclose(time_to_seconds(dt), [0, 0.1, 0.2])
    np.testing.assert_allclose(time_to_seconds(pd.Series([0.0, 0.01, 0.02]))[2], 0.02)
