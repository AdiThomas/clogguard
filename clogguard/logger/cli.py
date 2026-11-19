"""``clogguard log``: record every print on the printer as a run directory."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import get_args

from clogguard.core.channels import ChannelConfig
from clogguard.core.config import ServiceConfig
from clogguard.core.schema import RunPurpose
from clogguard.logger.run_logger import RunLogger, http_snapshot
from clogguard.moonraker import MoonrakerClient


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clogguard log", description=__doc__)
    p.add_argument("--service-config", required=True, help="service YAML (Moonraker URL, ...)")
    p.add_argument("--channels", help="channel YAML (default: channels_file in service config)")
    p.add_argument("--purpose", default="other", choices=list(get_args(RunPurpose)))
    p.add_argument("--operator", default="")
    p.add_argument("--printer", default="")
    p.add_argument("--runs-dir", help="override runs_dir from the service config")
    p.add_argument("--webcam-url", help="snapshot URL; enables photos")
    p.add_argument("--photo-interval", type=float, default=None, help="seconds between photos")
    p.add_argument("--photo-on-layer", action="store_true", help="photo at every layer change")
    p.add_argument("--flush-interval", type=float, default=10.0, help="seconds between flushes")
    p.add_argument("--max-runs", type=int, default=None, help="exit after N finished prints")
    p.add_argument("--no-raw", action="store_true", help="do not keep raw fast-channel samples")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def _run(args: argparse.Namespace) -> int:
    service = ServiceConfig.load(args.service_config)
    channels = ChannelConfig.load(args.channels or service.channels_file)
    client = MoonrakerClient(service.moonraker_url, api_key_env=service.api_key_env)
    await client.connect(retry=True)
    logger = RunLogger.from_config(
        client,
        service,
        channels,
        runs_dir=args.runs_dir or service.runs_dir,
        purpose=args.purpose,
        operator=args.operator,
        printer=args.printer,
        snapshot=http_snapshot(args.webcam_url) if args.webcam_url else None,
        photo_interval_s=args.photo_interval,
        photo_on_layer=args.photo_on_layer,
        flush_interval_s=args.flush_interval,
        keep_raw=not args.no_raw,
    )
    try:
        runs = await logger.run(max_runs=args.max_runs)
    finally:
        await client.close()
    for r in runs:
        print(r)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
