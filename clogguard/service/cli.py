"""``clogguard serve``: run the real-time service against Moonraker.

    clogguard serve --config config/service.yaml --channels config/channels.yaml [--model PATH]

The model file is a joblib dump of a ``HealthModel``; the service calls ``make_online()`` once
per print. ``--model`` defaults to ``detector.model_path`` from the service config.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from clogguard.core.channels import ChannelConfig
from clogguard.core.config import ServiceConfig


def load_model(path: Path) -> Any:
    """Load a joblib model and check it can make online scorers. Raises SystemExit(2) with a
    clear message otherwise."""
    if not path.is_file():
        print(
            f"clogguard serve: model file not found: {path}\n"
            "Train one with `clogguard train` or pass --model PATH.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    import joblib

    model = joblib.load(path)
    if not callable(getattr(model, "make_online", None)):
        print(f"clogguard serve: {path} has no make_online(); not a HealthModel",
              file=sys.stderr)
        raise SystemExit(2)
    return model


async def _serve(config: ServiceConfig, channels: ChannelConfig, model: Any) -> None:
    from clogguard.service.link import MoonrakerLink
    from clogguard.service.runtime import Service

    link = MoonrakerLink.from_config(config)
    service = Service(link, channels, config, model.make_online)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, service.stop)
    await link.start()
    try:
        await service.run()
    finally:
        await link.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clogguard serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/service.yaml", type=Path)
    ap.add_argument("--channels", default=None, type=Path,
                    help="channel config (default: channels_file from the service config)")
    ap.add_argument("--model", default=None, type=Path)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for p in (args.config, args.channels):
        if p is not None and not p.is_file():
            print(f"clogguard serve: file not found: {p}", file=sys.stderr)
            return 2
    config = ServiceConfig.load(args.config)
    channels_path = args.channels or Path(config.channels_file)
    if not channels_path.is_file():
        print(f"clogguard serve: file not found: {channels_path}", file=sys.stderr)
        return 2
    channels = ChannelConfig.load(channels_path)
    try:
        model = load_model(args.model or Path(config.detector.model_path))
    except SystemExit as exc:
        return int(exc.code or 2)
    try:
        asyncio.run(_serve(config, channels, model))
    except RuntimeError as exc:
        print(f"clogguard serve: {exc}", file=sys.stderr)
        return 2
    return 0
