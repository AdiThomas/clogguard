import json

import numpy as np
import pandas as pd
import pytest

from clogguard.core.channels import ChannelConfig, ChannelSpec
from clogguard.core.schema import DefectRating, Run
from clogguard.store import (
    PARTS_DIR,
    PartWriter,
    RunStore,
    consolidate_parts,
    load_raw,
    load_run,
    read_meta,
    save_run,
    validate_run,
)
from tests.data_platform.helpers import make_run, photos


def codes(issues):
    return {i.code for i in issues}


def test_roundtrip_and_filters(tmp_path):
    store = RunStore(tmp_path)
    assert store.list() == [] and list(RunStore(tmp_path / "nope").run_dirs()) == []
    a = make_run("run_a")
    b = make_run(
        "run_b",
        induction="temp_step",
        purpose="induction_trial",
        manipulated_variables=["extruder_target_c"],
    )
    c = make_run("sim_c", data_source="simulated", induction="simulated", purpose="other")
    store.save(a)
    store.save(b)
    save_run(c, tmp_path / "sim" / "batch1" / "sim_c")  # nested discovery
    assert store.run_ids() == ["run_a", "run_b", "sim_c"]
    loaded = store.load("run_a")
    pd.testing.assert_frame_equal(loaded.frame, a.frame)
    assert loaded.meta == a.meta
    assert store.load("sim_c").meta.data_source == "simulated"
    assert [m.run_id for m in store.list(purpose="induction_trial")] == ["run_b"]
    assert [m.run_id for m in store.list(data_source="real")] == ["run_a", "run_b"]
    assert [m.run_id for m in store.list(induction={"temp_step", "simulated"})] == [
        "run_b",
        "sim_c",
    ]
    runs = store.iter_runs(data_source="simulated")
    assert next(runs).run_id == "sim_c"
    assert "run_a" in store and "zzz" not in store
    with pytest.raises(KeyError):
        store.load("zzz")
    with pytest.raises(FileExistsError):
        store.save(a)
    store.save(a, overwrite=True)


def test_duplicate_and_unreadable(tmp_path, caplog):
    store = RunStore(tmp_path)
    save_run(make_run("dup"), tmp_path / "x" / "one")
    save_run(make_run("dup"), tmp_path / "y" / "two")
    with pytest.raises(KeyError, match="duplicated"):
        store.path("dup")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "meta.json").write_text("{not json")
    hidden = tmp_path / ".trash" / "old"
    save_run(make_run("hidden"), hidden)
    assert store.run_ids() == ["dup", "dup"]
    assert "unreadable" in caplog.text


def test_update_meta(tmp_path):
    store = RunStore(tmp_path)
    store.save(make_run("run_a"))
    meta = store.update_meta(
        "run_a", part_mass_g=12.5, defect_ratings=[DefectRating(rater="A", t_s=1.0)]
    )
    assert meta.part_mass_g == 12.5
    assert store.load_meta("run_a").defect_ratings[0].rater == "A"
    with pytest.raises(ValueError, match="unknown"):
        store.update_meta("run_a", bogus=1)
    with pytest.raises(ValueError, match="run_id"):
        store.update_meta("run_a", run_id="other")
    with pytest.raises(ValueError):
        store.update_meta("run_a", data_source="open_dataset")  # citation required
    assert read_meta(store.path("run_a")).data_source == "real"


def test_parts_crash_recovery(tmp_path):
    run = make_run("crashy")
    d = tmp_path / "crashy"
    save_run(run, d)
    (d / "timeseries.parquet").unlink()  # writer died before consolidating
    w = PartWriter(d, "timeseries")
    assert w.write(pd.DataFrame()) is None
    w.write(run.frame.iloc[:20])
    w.write(run.frame.iloc[20:])
    raw = PartWriter(d, "raw/force_g")
    raw.write(pd.DataFrame({"t": [0.0, 0.01], "value": [1.0, 2.0]}))
    loaded = load_run(d)  # loads from parts without modifying them
    pd.testing.assert_frame_equal(loaded.frame, run.frame)
    assert len(load_raw(d, "force_g")) == 2
    assert PartWriter(d, "timeseries").part_paths()
    out = consolidate_parts(d)
    assert sorted(p.name for p in out) == ["force_g.parquet", "timeseries.parquet"]
    assert not (d / PARTS_DIR).exists()
    pd.testing.assert_frame_equal(load_run(d).frame, run.frame)
    assert consolidate_parts(d) == []
    assert load_raw(d, "accel") is None
    # appending more parts after consolidation extends the final file
    w2 = PartWriter(d, "timeseries")
    extra = pd.DataFrame({"t": [5.0], "extruder_power": [0.5], "extruder_target_c": [210.0]})
    w2.write(extra)
    w2.consolidate()
    assert len(load_run(d).frame) == len(run.frame) + 1
    assert PartWriter(d, "nothing").consolidate() is None


def test_empty_frame_loads(tmp_path):
    d = tmp_path / "e"
    d.mkdir()
    (d / "meta.json").write_text(make_run("e").meta.model_dump_json())
    assert list(load_run(d).frame.columns) == ["t"]


def test_valid_run_has_no_issues(tmp_path):
    run = make_run(photos=photos([1.0, 2.0]), defect_ratings=[DefectRating(rater="A", t_s=2.0)])
    assert validate_run(run) == []


def test_rule_monotonic_and_gaps():
    run = make_run()
    f = run.frame.copy()
    f.loc[10, "t"] = f.loc[9, "t"]  # duplicate timestamp
    f = f.drop(index=range(20, 30)).reset_index(drop=True)  # 1.1 s gap
    issues = validate_run(Run(run.meta, f))
    assert {"t_not_monotonic", "base_rate_gap"} <= codes(issues)
    gap = next(i for i in issues if i.code == "base_rate_gap")
    assert gap.severity == "warning" and "1.10" in str(gap)
    f2 = run.frame.copy()
    f2.loc[3, "t"] = np.nan
    assert "t_nan" in codes(validate_run(Run(run.meta, f2)))
    assert "no_time_column" in codes(validate_run(Run(run.meta, run.frame.drop(columns="t"))))
    assert "empty_timeseries" in codes(validate_run(Run(run.meta, run.frame.iloc[:0])))


def test_rule_declared_channels():
    run = make_run()
    issues = validate_run(Run(run.meta, run.frame.drop(columns="extruder_power")))
    assert "declared_channel_missing" in codes(issues)


def test_rule_label_channels_only_simulated():
    real = make_run()
    f = real.frame.assign(true_restriction=0.0)
    assert "label_channel_in_non_simulated" in codes(validate_run(Run(real.meta, f)))
    sim = make_run(data_source="simulated", induction="simulated")
    assert validate_run(Run(sim.meta, f)) == []
    # explicit channel config decides which columns are labels
    cfg = ChannelConfig(
        channels=[ChannelSpec(name="severity", unit="1", rate_hz=10, source="sim", role="label")]
    )
    f2 = real.frame.assign(severity=0.1)
    assert "label_channel_in_non_simulated" in codes(validate_run(Run(real.meta, f2), channels=cfg))
    assert validate_run(Run(real.meta, f2)) == []


def test_rule_defect_ratings():
    run = make_run(photos=photos([1.0, 2.0]))
    meta = run.meta.model_copy(
        update={
            "defect_ratings": [
                DefectRating(rater="A", t_s=99.0),  # beyond 4.9 s run
                DefectRating(rater="A", t_s=None),  # duplicate rater
                DefectRating(rater=" ", t_s=None),
                DefectRating(rater="B", t_s=-1.0),
                DefectRating(rater="C", t_s=1.5),  # not a photo time
            ]
        }
    )
    c = codes(validate_run(Run(meta, run.frame)))
    assert {
        "rating_out_of_range",
        "rating_duplicate_rater",
        "rating_no_rater",
        "rating_not_on_photo",
    } <= c


def test_rule_citation_and_manipulated():
    run = make_run(data_source="open_dataset", source_citation="Doe 2024")
    run.meta.source_citation = ""  # bypasses construction-time check, as a mutated meta could
    assert "missing_citation" in codes(validate_run(run))
    clog = make_run(induction="temp_step", manipulated_variables=["extruder_target_c", "fan_speed"])
    issues = validate_run(clog)
    assert "manipulated_variable_missing" in codes(issues)
    assert "fan_speed" in next(str(i) for i in issues if i.code == "manipulated_variable_missing")


def test_rule_photo_files(tmp_path):
    run = make_run(photos=photos([1.0]))
    save_run(run, tmp_path / "r")
    assert "photo_missing" in codes(validate_run(run, run_dir=tmp_path / "r"))
    (tmp_path / "r" / "photos").mkdir()
    (tmp_path / "r" / "photos" / "0001.jpg").write_bytes(b"x")
    assert validate_run(run, run_dir=tmp_path / "r") == []
    assert json.loads((tmp_path / "r" / "meta.json").read_text())["run_id"] == "run_a"
