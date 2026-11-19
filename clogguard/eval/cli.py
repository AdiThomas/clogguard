"""``clogguard eval``: Table 2 report from stored runs.

    clogguard eval table2 --runs runs/ --bom bom.csv --install install.csv --out report.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from clogguard.eval.report import MixedSourceError, build_table2
from clogguard.eval.runs import load_runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clogguard eval", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    t2 = sub.add_parser("table2", help="Table 2 criteria report (Markdown + JSON)")
    t2.add_argument("--runs", nargs="+", required=True, type=Path, help="run roots")
    t2.add_argument("--bom", type=Path, help="BOM CSV: item, qty, unit_cost_cad, url")
    t2.add_argument("--install", type=Path, help="install trials CSV with a minutes column")
    t2.add_argument("--out", type=Path, default=Path("table2.md"), help="Markdown output")
    t2.add_argument("--json", type=Path, help="JSON output (default: --out with .json)")
    t2.add_argument("--allow-mixed", action="store_true", help="one section per data source")
    t2.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)

    runs = load_runs(ns.runs, with_frame=False)
    if not runs:
        print("no run directories (with meta.json) found", file=sys.stderr)
        return 1
    try:
        rep = build_table2(runs, ns.bom, ns.install, allow_mixed=ns.allow_mixed, seed=ns.seed)
    except MixedSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ns.out.parent.mkdir(parents=True, exist_ok=True)
    ns.out.write_text(rep.to_markdown(), encoding="utf-8")
    js = ns.json or ns.out.with_suffix(".json")
    js.write_text(rep.to_json(), encoding="utf-8")
    print(f"{rep.title}\nwrote {ns.out} and {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
