"""Standard-library downloader for the open datasets.

Files go to ``data/open/<name>/``. Partial downloads are kept as ``<file>.part`` and resumed with
an HTTP Range request. After download every file's SHA-256 is recorded in ``checksums.json`` and
checked against the registry value if one is set; ``provenance.json`` records URLs, date, licence
and citation.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from clogguard.datasets.registry import DatasetInfo, RemoteFile, get

DEFAULT_ROOT = Path("data/open")
CHUNK = 1 << 20
USER_AGENT = "clogguard-data/0.1 (python urllib)"


class ChecksumError(RuntimeError):
    pass


class ManualDownloadRequired(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _request(url: str, start: int = 0) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if start:
        req.add_header("Range", f"bytes={start}-")
    return req


def fetch(url: str, dest: Path, timeout: float = 60.0) -> Path:
    """Download ``url`` to ``dest``, resuming ``dest.part`` if present."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    with urllib.request.urlopen(_request(url, start), timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        mode = "ab" if (start and status == 206) else "wb"  # server ignored Range -> restart
        with part.open(mode) as f:
            while block := resp.read(CHUNK):
                f.write(block)
    part.replace(dest)
    return dest


def extract_zip(archive: Path, dest_dir: Path) -> list[Path]:
    """Extract an archive, refusing members that would land outside ``dest_dir``."""
    out: list[Path] = []
    base = dest_dir.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (dest_dir / member.filename).resolve()
            if not target.is_relative_to(base):
                raise ValueError(f"unsafe path in archive: {member.filename}")
            if member.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                while block := src.read(CHUNK):
                    dst.write(block)
            out.append(target)
    return out


def zenodo_files(record_id: str, timeout: float = 60.0) -> list[RemoteFile]:  # pragma: no cover
    """List a Zenodo record's files via its public API (needs internet)."""
    url = f"https://zenodo.org/api/records/{record_id}"
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
        data = json.load(resp)
    # ASSUMPTION: Zenodo API v2 shape: files[].key and files[].links.self.
    return [RemoteFile(name=f["key"], url=f["links"]["self"]) for f in data.get("files", [])]


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_provenance(info: DatasetInfo, dest_dir: Path, files: list[RemoteFile]) -> dict:
    """Hash every data file in ``dest_dir`` and write checksums.json + provenance.json."""
    known = {f.name: f for f in files}
    checksums: dict[str, str] = {}
    entries = []
    for p in sorted(dest_dir.rglob("*")):
        if not p.is_file() or p.name in ("checksums.json", "provenance.json"):
            continue
        if p.name.endswith(".part"):
            continue
        rel = p.relative_to(dest_dir).as_posix()
        digest = sha256_file(p)
        checksums[rel] = digest
        entries.append(
            {
                "file": rel,
                "bytes": p.stat().st_size,
                "sha256": digest,
                "url": known[rel].url if rel in known else None,
            }
        )
    prov = {
        "dataset": info.name,
        "title": info.title,
        "landing_url": info.landing_url,
        "doi": info.doi,
        "licence": info.licence,
        "citation": info.citation,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": entries,
    }
    _write_json(dest_dir / "checksums.json", checksums)
    _write_json(dest_dir / "provenance.json", prov)
    return prov


def download(
    name: str,
    root: str | Path = DEFAULT_ROOT,
    files: list[RemoteFile] | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download dataset ``name`` into ``root/name``. ``files`` overrides the registry list."""
    info = get(name)
    dest_dir = Path(root) / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    if files is None:
        files = list(info.files)
        if not files and info.zenodo_record:
            files = zenodo_files(info.zenodo_record, timeout=timeout)  # pragma: no cover
    if not files:
        existing = [p for p in dest_dir.rglob("*") if p.is_file() and p.suffix != ".json"]
        if not existing:
            raise ManualDownloadRequired(
                f"{name}: no direct download links. {info.manual_steps or ''} "
                f"Landing page: {info.landing_url}"
            )
    for rf in files:
        target = dest_dir / rf.name
        fresh = not target.exists()
        if fresh:
            fetch(rf.url, target, timeout=timeout)
        if rf.sha256:
            got = sha256_file(target)
            if got != rf.sha256:
                bad = target.with_name(target.name + ".bad")
                target.replace(bad)
                raise ChecksumError(
                    f"{rf.name}: sha256 {got} does not match registry {rf.sha256}; "
                    f"kept as {bad.name}"
                )
        if fresh and target.suffix == ".zip":
            extract_zip(target, dest_dir)
    record_provenance(info, dest_dir, files)
    return dest_dir
