"""Command-line entry point. Workstreams register subcommands in ``SUBCOMMANDS``."""

from __future__ import annotations

import argparse
import importlib
import sys

# name -> "module:function". Each function takes (argv: list[str]) -> int.
SUBCOMMANDS: dict[str, str] = {
    "log": "clogguard.logger.cli:main",
    "sim": "clogguard.sim.cli:main",
    "mock-printer": "clogguard.sim.mock_moonraker:main",
    "train": "clogguard.train.cli:main",
    "compare": "clogguard.train.compare:main",
    "serve": "clogguard.service.cli:main",
    "campaign": "clogguard.campaign.cli:main",
    "eval": "clogguard.eval.cli:main",
    "label": "clogguard.labeling.cli:main",
    "data": "clogguard.datasets.cli:main",
    "dashboard": "clogguard.dashboard.app:main",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="clogguard", description=__doc__)
    parser.add_argument("command", choices=sorted(SUBCOMMANDS))
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    ns, _ = parser.parse_known_args(argv[:1])
    module_name, func_name = SUBCOMMANDS[ns.command].split(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        print(f"clogguard {ns.command}: not available ({exc})", file=sys.stderr)
        return 2
    return int(getattr(module, func_name)(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
