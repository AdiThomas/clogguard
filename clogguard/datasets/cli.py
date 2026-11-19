"""``clogguard data list|info|download <name>|convert <name>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from clogguard.datasets import agh, caxton, tud, ufrgs
from clogguard.datasets.download import (
    DEFAULT_ROOT,
    ChecksumError,
    ManualDownloadRequired,
    download,
)
from clogguard.datasets.registry import REGISTRY, get
from clogguard.datasets.writer import write_run

DEFAULT_RUNS = Path("runs/open")


def _info(name: str) -> str:
    d = get(name)
    lines = [
        f"{d.name}: {d.title}",
        f"  URL:      {d.landing_url}",
        *(f"  also:     {u}" for u in d.extra_urls),
        f"  DOI:      {d.doi or '-'}",
        f"  Licence:  {d.licence}",
        f"  Citation: {d.citation}",
        f"  Files:    {', '.join(d.expected_files)}",
        f"  Notes:    {d.notes}",
    ]
    if d.manual_steps:
        lines.append(f"  Manual:   {d.manual_steps}")
    for f in d.files:
        lines.append(f"  file {f.name}: {f.url} sha256={f.sha256 or 'not yet recorded'}")
    return "\n".join(lines)


def _convert(name: str, src: Path, out: Path, limit: int | None, rate_hz: float | None) -> int:
    if name == "caxton":
        labels = caxton.load_labels(caxton.find_label_csv(src), max_rows=limit)
        counts = labels["flow_rate_class"].value_counts(dropna=False).to_dict()
        out.mkdir(parents=True, exist_ok=True)
        labels.to_parquet(out / "labels.parquet", index=False)
        dest = out / "labels.parquet"
        print(f"caxton: {len(labels)} label rows -> {dest}; flow classes {counts}")
        print("caxton: images are not converted (labels only).")
        return 0
    if name == "ufrgs_vibration":
        runs = ufrgs.load_dir(src, limit=limit)
    elif name == "tud_force":
        runs = tud.load_dir(src, rate_hz=rate_hz, limit=limit)
    elif name == "agh_anomaly":
        runs = agh.load_dir(src, rate_hz=rate_hz, limit=limit)
    else:  # pragma: no cover - guarded by argparse choices
        raise KeyError(name)
    if not runs:
        print(f"{name}: no input files found in {src}", file=sys.stderr)
        return 1
    for r in runs:
        d = write_run(r, out)
        print(f"{r.run_id}: {len(r.frame)} rows at 10 Hz -> {d}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clogguard data", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list registered datasets")
    p_info = sub.add_parser("info", help="show URL, licence and citation")
    p_info.add_argument("name", choices=sorted(REGISTRY))
    p_dl = sub.add_parser("download", help="download into data/open/<name>/")
    p_dl.add_argument("name", choices=sorted(REGISTRY))
    p_dl.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p_cv = sub.add_parser("convert", help="convert to runs in runs/open/<name>/")
    p_cv.add_argument("name", choices=sorted(REGISTRY))
    p_cv.add_argument("--src", type=Path, default=None, help="default data/open/<name>")
    p_cv.add_argument("--out", type=Path, default=None, help="default runs/open/<name>")
    p_cv.add_argument("--limit", type=int, default=None, help="convert at most N files")
    p_cv.add_argument("--rate-hz", type=float, default=None, help="sample rate if no time column")
    ns = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if ns.cmd == "list":
        for d in REGISTRY.values():
            print(f"{d.name:16s} {d.licence:38s} {d.landing_url}")
        return 0
    if ns.cmd == "info":
        print(_info(ns.name))
        return 0
    if ns.cmd == "download":
        try:
            d = download(ns.name, root=ns.root)
        except ManualDownloadRequired as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except ChecksumError as exc:
            print(f"checksum failure: {exc}", file=sys.stderr)
            return 4
        print(f"{ns.name}: files, checksums.json and provenance.json in {d}")
        return 0
    src = ns.src or DEFAULT_ROOT / ns.name
    out = ns.out or DEFAULT_RUNS / ns.name
    return _convert(ns.name, src, out, ns.limit, ns.rate_hz)


if __name__ == "__main__":
    raise SystemExit(main())
