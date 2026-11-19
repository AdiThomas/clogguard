from __future__ import annotations

import hashlib
import http.server
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from benchmarks import ufrgs_vibration as bench
from clogguard.datasets import cli, download
from clogguard.datasets.registry import RemoteFile

PAYLOAD = bytes(range(256)) * 400  # 102,400 bytes


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    files: ClassVar[dict[str, bytes]] = {}
    ranges_seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        body = self.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng:
            self.ranges_seen.append(rng)
            start = int(rng.split("=")[1].split("-")[0])
            chunk = body[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            chunk = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("repo-master/data/a.csv", "t,x\n0,1\n")
    _RangeHandler.files = {"/data.bin": PAYLOAD, "/repo.zip": zbuf.getvalue()}
    _RangeHandler.ranges_seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_download_resume_checksum_provenance(server: str, tmp_path: Path) -> None:
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    dest = tmp_path / "agh_anomaly"
    dest.mkdir()
    (dest / "data.bin.part").write_bytes(PAYLOAD[:1000])  # interrupted earlier
    files = [
        RemoteFile("data.bin", f"{server}/data.bin", sha256=digest),
        RemoteFile("repo.zip", f"{server}/repo.zip"),
    ]
    out = download.download("agh_anomaly", root=tmp_path, files=files)
    assert out == dest
    assert (dest / "data.bin").read_bytes() == PAYLOAD
    assert _RangeHandler.ranges_seen == ["bytes=1000-"]
    assert (dest / "repo-master/data/a.csv").exists()  # zip extracted
    sums = json.loads((dest / "checksums.json").read_text())
    assert sums["data.bin"] == digest
    prov = json.loads((dest / "provenance.json").read_text())
    assert prov["licence"] == "CC BY 4.0" and prov["citation"].startswith("Sendorek")
    assert {e["file"] for e in prov["files"]} >= {"data.bin", "repo.zip"}
    assert prov["retrieved_at"]


def test_download_checksum_mismatch(server: str, tmp_path: Path) -> None:
    files = [RemoteFile("data.bin", f"{server}/data.bin", sha256="0" * 64)]
    with pytest.raises(download.ChecksumError):
        download.download("ufrgs_vibration", root=tmp_path, files=files)
    assert (tmp_path / "ufrgs_vibration" / "data.bin.bad").exists()


def test_download_manual_and_unsafe_zip(tmp_path: Path) -> None:
    with pytest.raises(download.ManualDownloadRequired, match="tudatalib"):
        download.download("tud_force", root=tmp_path)
    assert cli.main(["download", "caxton", "--root", str(tmp_path)]) == 3
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        download.extract_zip(bad, tmp_path / "x")


def test_cli_list_info_convert(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    assert "ufrgs_vibration" in capsys.readouterr().out
    assert cli.main(["info", "tud_force"]) == 0
    assert "10.48328/tudatalib-1772" in capsys.readouterr().out
    src = tmp_path / "src"
    bench.make_fixture(src, n_per_class=1, seconds=2.0)
    out = tmp_path / "runs"
    assert cli.main(["convert", "ufrgs_vibration", "--src", str(src), "--out", str(out)]) == 0
    metas = sorted(out.glob("*/meta.json"))
    assert len(metas) == 7  # 6 files, one holding two prints
    assert json.loads(metas[0].read_text())["data_source"] == "open_dataset"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["convert", "agh_anomaly", "--src", str(empty), "--out", str(out)]) == 1


def test_benchmark_fixture_end_to_end(tmp_path: Path) -> None:
    rc = bench.main(
        [
            "--fixture",
            "--out-dir",
            str(tmp_path),
            "--fixture-per-class",
            "2",
            "--fixture-seconds",
            "8",
        ]
    )
    assert rc == 0
    md = (tmp_path / "ufrgs_vibration_FIXTURE.md").read_text()
    assert "SYNTHETIC" in md and "not ClogGuard hardware" in md
    assert "## Confusion matrix" in md and "| **nozzle_02** |" in md
    assert "leave-one-print-out" in md
    assert not (tmp_path / "ufrgs_vibration.md").exists()


def test_benchmark_real_header_and_missing_data(tmp_path: Path) -> None:
    src = tmp_path / "src"
    bench.make_fixture(src, n_per_class=2, seconds=6.0, seed=1)
    out = tmp_path / "real.md"
    res = bench.run(src, out, False, 5.0, 2.5, "rf", 0)
    md = out.read_text()
    assert bench.REAL_HEADER in md and "Citation:" in md
    assert res.n_prints == 13
    assert len(res.y_true) == len(res.y_pred) and set(res.y_true) <= set(bench.CLASSES)
    with pytest.raises(FileNotFoundError):
        bench.run(tmp_path / "none", out, False, 5.0, 2.5, "hgb", 0)
    assert np.isfinite(bench.build_dataset(bench.ufrgs.load_dir(src), 5.0, 2.5)[0].to_numpy()).all()


@pytest.mark.network
def test_zenodo_listing_live() -> None:  # pragma: no cover - needs internet
    files = download.zenodo_files("22716165")
    names = [f.name for f in files if f.name.endswith(".parquet")]
    assert len(names) >= 26
