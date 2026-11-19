import base64
import json
import re

import pytest

from clogguard.core.schema import DefectRating
from clogguard.labeling import apply_ratings, build_page, run_token
from clogguard.labeling.cli import main as label_main
from clogguard.store import read_meta, save_run
from tests.data_platform.helpers import make_run, photos

TIMES = [37.25, 12.5, 61.75]  # deliberately unsorted in meta


@pytest.fixture
def run_dir(tmp_path):
    run = make_run(
        "20261119T120000_clog_temp_step",
        induction="temp_step",
        manipulated_variables=["extruder_target_c"],
        photos=photos(TIMES),
        alert_time_s=44.4,
    )
    d = tmp_path / run.run_id
    save_run(run, d)
    (d / "photos").mkdir()
    for i in range(1, 4):
        (d / "photos" / f"{i:04d}.jpg").write_bytes(f"JPEGDATA{i}".encode())
    return d


def test_page_is_blind(run_dir):
    page = build_page(run_dir)
    for t in TIMES:
        assert str(t) not in page
    for leak in (
        "t_s",
        "44.4",
        "temp_step",
        "clog",
        "20261119",
        "extruder",
        "0.3",
        "photos/",
        "0001.jpg",
        "alert_time",
        "induction",
    ):
        assert leak not in page, leak
    # photos embedded in time order: t=12.5 (file 2), 37.25 (file 1), 61.75 (file 3)
    blobs = re.findall(r'src="data:image/jpeg;base64,([^"]+)"', page)
    assert [base64.b64decode(b) for b in blobs] == [b"JPEGDATA2", b"JPEGDATA1", b"JPEGDATA3"]
    assert 'id="rater"' in page and "First defect visible here" in page
    assert "No defect in any photo" in page
    assert json.dumps(run_token(read_meta(run_dir))) in page
    assert "http://" not in page and "https://" not in page  # self-contained


def test_page_requires_photos(tmp_path):
    save_run(make_run("nophotos"), tmp_path / "n")
    with pytest.raises(ValueError, match="no photos"):
        build_page(tmp_path / "n")


def _rating(run_dir, rater, idx):
    out = {
        "format": "blind-defect-rating",
        "version": 1,
        "run_token": run_token(read_meta(run_dir)),
        "n_photos": 3,
        "rater": rater,
        "first_defect_photo": idx,
    }
    return out


def test_apply_maps_indices_to_times(run_dir, tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps(_rating(run_dir, "Alice", 2)))
    meta = apply_ratings(run_dir, f)
    assert meta.defect_ratings == [DefectRating(rater="Alice", t_s=37.25)]
    meta = apply_ratings(run_dir, [_rating(run_dir, "Bob", None), _rating(run_dir, "alice ", 1)])
    by = {r.rater: r.t_s for r in meta.defect_ratings}
    assert by == {"Bob": None, "alice": 12.5}  # Alice re-rated: replaced, not duplicated
    assert read_meta(run_dir).defect_onset_s == 12.5


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"format": "x"}, "not a"),
        ({"run_token": "0000"}, "different run"),
        ({"n_photos": 2}, "photo count"),
        ({"rater": ""}, "rater"),
        ({"first_defect_photo": 4}, "out of range"),
        ({"first_defect_photo": True}, "out of range"),
    ],
)
def test_apply_rejects_bad_files(run_dir, bad, match):
    with pytest.raises(ValueError, match=match):
        item = _rating(run_dir, "Alice", 1)
        item.update(bad)
        apply_ratings(run_dir, item)
    with pytest.raises(ValueError, match="object"):
        apply_ratings(run_dir, [1, 2])


def test_cli(run_dir, tmp_path, capsys):
    out = tmp_path / "page" / "rate.html"
    assert label_main(["build", str(run_dir), "--out", str(out)]) == 0
    assert out.read_text().startswith("<!DOCTYPE html>")
    f = tmp_path / "r.json"
    f.write_text(json.dumps(_rating(run_dir, "Alice", 3)))
    assert label_main(["apply", str(run_dir), str(f)]) == 0
    assert "61.75" in capsys.readouterr().out
    f.write_text("{}")
    assert label_main(["apply", str(run_dir), str(f)]) == 1
