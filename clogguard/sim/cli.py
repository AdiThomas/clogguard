"""``clogguard sim``: generate SIMULATED runs in the core run layout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clogguard.sim.io import write_run
from clogguard.sim.scenarios import PRESETS, generate_dataset, generate_run, preset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clogguard sim", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="generate a mixed dataset of simulated runs")
    g.add_argument("--n-normal", type=int, default=20)
    g.add_argument("--n-clog", type=int, default=20)
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("--out", type=Path, default=Path("runs/sim"))
    g.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("preset", help="generate one run from a named preset")
    p.add_argument("name", choices=sorted(PRESETS))
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path("runs/sim"))
    p.add_argument("--overwrite", action="store_true")
    sub.add_parser("list", help="list presets")
    ns = ap.parse_args(argv)

    if ns.cmd == "list":
        for name, sc in PRESETS.items():
            print(f"{name:16s} {sc.geometry:9s} {sc.clog_type:10s} {sc.profile}")
        return 0
    if ns.cmd == "preset":
        runs = [generate_run(preset(ns.name, seed=ns.seed))]
    else:
        runs = generate_dataset(ns.n_normal, ns.n_clog, ns.seed)
    ns.out.mkdir(parents=True, exist_ok=True)
    for run in runs:
        d = write_run(run, ns.out, overwrite=ns.overwrite)
        print(json.dumps({"run_id": run.run_id, "dir": str(d),
                          "clog": run.meta.is_clog_run,
                          "defect_onset_s": run.meta.defect_onset_s,
                          "data_source": run.meta.data_source}))
    print(f"wrote {len(runs)} SIMULATED runs to {ns.out}")
    return 0
