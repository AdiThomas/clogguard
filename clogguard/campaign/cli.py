"""``clogguard campaign``: plan, checklist, report.

    clogguard campaign plan --out campaign/ [--config plan.yaml] [--seed N] [--block-length 4]
    clogguard campaign plan --write-default plan.yaml
    clogguard campaign checklist --run-dir runs/CG-001-BL --datasheet-max-c 220 [--yes]
    clogguard campaign checklist --markdown checklist.md
    clogguard campaign report runs/CG-001-BL [--out report.html]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from clogguard.campaign.checklist import (
    ChecklistError,
    checklist_markdown,
    record_completion,
    run_checklist,
)
from clogguard.campaign.plan import PlanConfig, default_plan_yaml, generate_plan, write_outputs
from clogguard.core.schema import META_FILE, RunMetadata


def _plan(ns: argparse.Namespace) -> int:
    if ns.write_default:
        Path(ns.write_default).write_text(default_plan_yaml(), encoding="utf-8")
        print(f"wrote {ns.write_default}")
        return 0
    cfg = PlanConfig.load(ns.config)
    updates = {}
    if ns.seed is not None:
        updates["seed"] = ns.seed
    if ns.block_length is not None:
        updates["block_length"] = ns.block_length
    if updates:
        cfg = PlanConfig.model_validate({**cfg.model_dump(), **updates})
    runs = generate_plan(cfg)
    paths = write_outputs(runs, ns.out)
    print(f"{len(runs)} runs (seed {cfg.seed}); wrote {paths['csv']} and {paths['sheets_html']}")
    return 0


def _checklist(ns: argparse.Namespace) -> int:
    if ns.markdown:
        Path(ns.markdown).write_text(checklist_markdown(clog_run=True), encoding="utf-8")
        print(f"wrote {ns.markdown}")
        return 0
    if not ns.run_dir:
        print("error: --run-dir is required (or --markdown)", file=sys.stderr)
        return 2
    run_dir = Path(ns.run_dir)
    run_id = run_dir.name
    clog = ns.clog
    meta_path = run_dir / META_FILE
    if meta_path.is_file():
        meta = RunMetadata.model_validate_json(meta_path.read_text(encoding="utf-8"))
        run_id = meta.run_id
        clog = clog or meta.is_clog_run
    try:
        rec = run_checklist(
            run_id,
            clog_run=clog,
            datasheet_max_c=ns.datasheet_max_c,
            klipper_max_temp_c=ns.klipper_max_temp_c,
            supervisor=ns.supervisor or "",
            operator=ns.operator or "",
            assume_yes=ns.yes,
        )
    except (ChecklistError, EOFError) as exc:
        print(f"CHECKLIST NOT COMPLETE: {exc}", file=sys.stderr)
        return 1
    where = record_completion(run_dir, rec)
    print(f"checklist complete for {run_id}; recorded in {where}")
    return 0


def _report(ns: argparse.Namespace) -> int:
    from clogguard.campaign.run_report import write_run_report  # matplotlib import is slow

    out = write_run_report(ns.run_dir, ns.out)
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clogguard campaign", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="randomised run order, CSV and printable run sheets")
    p.add_argument("--config", type=Path, help="plan YAML (default: docs/plan.md test plan)")
    p.add_argument("--out", type=Path, default=Path("campaign"))
    p.add_argument("--seed", type=int)
    p.add_argument("--block-length", type=int)
    p.add_argument("--write-default", type=Path, help="write the default plan YAML and exit")
    p.set_defaults(func=_plan)

    c = sub.add_parser("checklist", help="pre-run safety checklist")
    c.add_argument("--run-dir", type=Path)
    c.add_argument("--yes", action="store_true", help="accept all confirmations (listed)")
    c.add_argument("--clog", action="store_true", help="clog run (supervisor required)")
    c.add_argument("--datasheet-max-c", type=float)
    c.add_argument("--klipper-max-temp-c", type=float)
    c.add_argument("--supervisor")
    c.add_argument("--operator")
    c.add_argument("--markdown", type=Path, help="write the printable checklist and exit")
    c.set_defaults(func=_checklist)

    r = sub.add_parser("report", help="per-run HTML report")
    r.add_argument("run_dir", type=Path)
    r.add_argument("--out", type=Path)
    r.set_defaults(func=_report)

    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
