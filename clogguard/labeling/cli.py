"""``clogguard label``: blind defect labelling.

clogguard label build <run_dir> --out page.html
clogguard label apply <run_dir> ratings.json [more.json ...]
"""

from __future__ import annotations

import argparse
import sys

from clogguard.labeling.blind import apply_ratings, write_page


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clogguard label", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="make the blind-rating HTML page for one run")
    b.add_argument("run_dir")
    b.add_argument("--out", required=True)
    a = sub.add_parser("apply", help="merge downloaded ratings into meta.json")
    a.add_argument("run_dir")
    a.add_argument("ratings", nargs="+")
    args = p.parse_args(argv)
    try:
        if args.cmd == "build":
            print(write_page(args.run_dir, args.out))
        else:
            meta = None
            for path in args.ratings:
                meta = apply_ratings(args.run_dir, path)
            assert meta is not None
            print(f"{len(meta.defect_ratings)} ratings; defect onset {meta.defect_onset_s} s")
    except (ValueError, OSError) as exc:
        print(f"clogguard label: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
