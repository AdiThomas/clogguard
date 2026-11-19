"""``RunLogger``: record every print on a real printer as a run directory.

Watches ``print_stats.state`` through a ``MoonrakerClient`` subscription. On the transition to
"printing" it opens ``runs/<run_id>/``, writes ``meta.json`` and appends base-rate rows (buffered
in memory, flushed as Parquet parts every ``flush_interval_s``, consolidated at the end, so a
crash loses at most that much). Optional layer photos come from an injectable async snapshot
function. ``audit.jsonl`` belongs to the service and is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from clogguard import __version__
from clogguard.channels import (
    ClockAligner,
    DerivedSource,
    MoonrakerSource,
    Sampler,
    SerialSource,
    SourceContext,
    build_sources,
    merge_subscriptions,
)
from clogguard.core.channels import ChannelConfig, ChannelSpec, SourceKind
from clogguard.core.config import MaterialProfile, ServiceConfig
from clogguard.core.schema import (
    BASE_RATE_HZ,
    META_FILE,
    PHOTO_DIR,
    RAW_DIR,
    PhotoRecord,
    RunMetadata,
    RunPurpose,
)
from clogguard.moonraker.status import get_path, merge_status
from clogguard.store.io import PartWriter, read_meta, write_meta

log = logging.getLogger(__name__)

SnapshotFn = Callable[[], Awaitable[bytes]]

PRINT_STATS_FIELDS = ["state", "filename", "print_duration", "info"]
END_OUTCOMES: dict[str, str] = {
    "complete": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}
# Fields the real-time service may write into the same meta.json; the logger keeps its values.
SERVICE_FIELDS = ("service_active", "service_mode", "alert_time_s", "actions")


class PrinterClient(Protocol):
    async def subscribe(self, objects: dict[str, Any]) -> dict[str, Any]: ...

    def updates(self) -> Any: ...


def make_run_id(started_at: datetime, filename: str) -> str:
    """``YYYYMMDDTHHMMSS_<gcode stem>``, restricted to the run-id character set."""
    stem = Path(filename).stem if filename else ""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-.") or "print"
    return f"{started_at:%Y%m%dT%H%M%S}_{safe}"[:80]


def combined_config_hash(service: ServiceConfig | None, channels: ChannelConfig) -> str:
    blob = (service.config_hash() if service else "") + channels.model_dump_json()
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def http_snapshot(url: str, timeout_s: float = 5.0) -> SnapshotFn:
    """Snapshot function doing an HTTP GET of a webcam snapshot URL (e.g. crowsnest/ustreamer
    ``http://<pi>/webcam/?action=snapshot``)."""

    async def snap() -> bytes:
        import httpx

        # trust_env=False: the webcam is on the LAN; never route it through an env proxy.
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    return snap


@dataclass
class _ActiveRun:
    run_dir: Path
    meta: RunMetadata
    t0: float
    writer: PartWriter
    raw_writers: dict[str, PartWriter]
    rows: list[dict[str, float]] = field(default_factory=list)
    last_flush: float = 0.0
    last_layer: Any = None
    tick_task: asyncio.Task[None] | None = None
    photo_task: asyncio.Task[None] | None = None
    photo_jobs: set[asyncio.Task[None]] = field(default_factory=set)
    photo_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    n_rows: int = 0


class RunLogger:
    def __init__(
        self,
        client: PrinterClient,
        channels: ChannelConfig,
        *,
        runs_dir: str | Path,
        material: MaterialProfile | None = None,
        config_hash: str = "",
        purpose: RunPurpose = "other",
        operator: str = "",
        printer: str = "",
        snapshot: SnapshotFn | None = None,
        photo_interval_s: float | None = None,
        photo_on_layer: bool = False,
        flush_interval_s: float = 10.0,
        keep_raw: bool = True,
        line_reader_factory: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        base_rate_hz: float = BASE_RATE_HZ,
    ) -> None:
        self.client = client
        self.channels = channels
        self.runs_dir = Path(runs_dir)
        self.config_hash = config_hash or combined_config_hash(None, channels)
        self.purpose: RunPurpose = purpose
        self.operator = operator
        self.printer = printer
        self.snapshot = snapshot
        self.photo_interval_s = photo_interval_s
        self.photo_on_layer = photo_on_layer
        self.flush_interval_s = flush_interval_s
        self.clock = clock
        self.wall_clock = wall_clock
        self.dt = 1.0 / base_rate_hz
        self.material_name = material.name if material else "PLA"

        ctx = SourceContext(material=material, line_reader_factory=line_reader_factory, clock=clock)
        sources = build_sources(channels, ctx, skip_unregistered=True)
        logged = {k for k in sources if k is not SourceKind.REPLAY}
        skipped = [c.name for c in channels.channels if c.source not in logged]
        if skipped:
            log.warning("channels not recordable by the logger, skipped: %s", skipped)
        self.specs: list[ChannelSpec] = [c for c in channels.channels if c.source in logged]
        self.moonraker = sources.get(SourceKind.MOONRAKER)
        self.serial = sources.get(SourceKind.SERIAL)
        derived = sources.get(SourceKind.DERIVED)
        assert self.moonraker is None or isinstance(self.moonraker, MoonrakerSource)
        assert self.serial is None or isinstance(self.serial, SerialSource)
        assert derived is None or isinstance(derived, DerivedSource)
        self.sampler = Sampler(
            self.specs, base_rate_hz=base_rate_hz, derived=derived, keep_raw=keep_raw
        )
        self._aligner = ClockAligner()
        self.status: dict[str, Any] = {}
        self.state: str | None = None
        self.active: _ActiveRun | None = None
        self.finished: list[Path] = []

    @classmethod
    def from_config(
        cls, client: PrinterClient, service: ServiceConfig, channels: ChannelConfig, **kw: Any
    ) -> RunLogger:
        kw.setdefault("runs_dir", service.runs_dir)
        return cls(
            client,
            channels,
            material=service.material,
            config_hash=combined_config_hash(service, channels),
            **kw,
        )

    # ------------------------------------------------------------------ main loop
    def subscription(self) -> dict[str, Any]:
        base: dict[str, list[str] | None] = {"print_stats": list(PRINT_STATS_FIELDS)}
        return merge_subscriptions(base, self.moonraker.subscription() if self.moonraker else {})

    async def run(
        self, *, max_runs: int | None = None, stop: asyncio.Event | None = None
    ) -> list[Path]:
        """Log prints until ``stop`` is set, ``max_runs`` runs have finished, or the update
        stream ends. Returns the finished run directories."""
        initial = await self.client.subscribe(self.subscription())
        await self._on_status_async(initial, None)
        tasks: dict[str, asyncio.Task[Any]] = {
            "updates": asyncio.create_task(self._consume(max_runs)),
        }
        if self.serial is not None:
            tasks["serial"] = asyncio.create_task(self.serial.run(self.sampler.push))
        if stop is not None:
            tasks["stop"] = asyncio.create_task(stop.wait())
        try:
            while True:
                done, _ = await asyncio.wait(
                    set(tasks.values()), return_when=asyncio.FIRST_COMPLETED
                )
                if tasks["updates"] in done:
                    tasks["updates"].result()
                    break
                if "stop" in tasks and tasks["stop"] in done:
                    break
                serial = tasks.pop("serial")
                serial.result()  # re-raises a reader failure (e.g. pyserial missing)
        finally:
            for t in tasks.values():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if self.active is not None:
                await self._finish(None, note="logger stopped before the print ended")
        return list(self.finished)

    async def _consume(self, max_runs: int | None) -> None:
        async for diff, eventtime in self.client.updates():
            await self._on_status_async(diff, eventtime)
            if max_runs is not None and len(self.finished) >= max_runs:
                return

    def _on_status(self, diff: dict[str, Any], eventtime: float | None) -> str | None:
        """Merge a diff and push channel values; return the new print state if it changed."""
        merge_status(self.status, diff)
        now = self.clock()
        t = now if eventtime is None else self._aligner.to_host(eventtime, now)
        if self.moonraker is not None:
            self.sampler.push_many(self.moonraker.extract(self.status), t)
        state = get_path(self.status, "print_stats", "state")
        if state != self.state:
            prev, self.state = self.state, state
            log.info("print state %s -> %s", prev, state)
            return str(state)
        return None

    async def _on_status_async(self, diff: dict[str, Any], eventtime: float | None) -> None:
        changed = self._on_status(diff, eventtime)
        if changed == "printing" and self.active is None:
            self._start()
        elif self.active is not None and changed in END_OUTCOMES:
            await self._finish(END_OUTCOMES[changed])
        elif self.active is not None and changed == "standby":
            # ASSUMPTION: printing -> standby without an end state means the print was aborted.
            await self._finish("cancelled", note="state went to standby without an end state")
        if self.active is not None:
            layer = get_path(self.status, "print_stats", "info", "current_layer")
            if layer != self.active.last_layer:
                self.active.last_layer = layer
                if self.photo_on_layer and layer is not None:
                    self._photo_soon()

    # ------------------------------------------------------------------ run lifecycle
    def _start(self) -> None:
        started = self.wall_clock()
        filename = str(get_path(self.status, "print_stats", "filename") or "")
        run_id = make_run_id(started, filename)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        notes = ""
        duration = get_path(self.status, "print_stats", "print_duration")
        if isinstance(duration, int | float) and duration > 5:
            notes = f"logger joined mid-print (print_duration {duration:.0f} s at t=0)"
        meta = RunMetadata(
            run_id=run_id,
            data_source="real",
            started_at=started,
            purpose=self.purpose,
            operator=self.operator,
            printer=self.printer,
            gcode_file=filename,
            material=self.material_name,
            channels=self.sampler.columns,
            software_version=__version__,
            config_hash=self.config_hash,
            notes=notes,
        )
        t0 = self.clock()
        self.sampler.reset(t0)
        raw_writers = {
            c: PartWriter(run_dir, f"{RAW_DIR}/{c}")
            for c in (self.sampler.raw.channels if self.sampler.raw is not None else [])
        }
        active = _ActiveRun(
            run_dir=run_dir,
            meta=meta,
            t0=t0,
            writer=PartWriter(run_dir, "timeseries"),
            raw_writers=raw_writers,
            last_flush=t0,
            last_layer=get_path(self.status, "print_stats", "info", "current_layer"),
        )
        self.active = active
        self._write_meta()
        log.info("run %s started in %s", run_id, run_dir)
        active.tick_task = asyncio.create_task(self._tick_loop(active))
        if self.snapshot is not None:
            self._photo_soon()
            if self.photo_interval_s:
                active.photo_task = asyncio.create_task(self._photo_loop(self.photo_interval_s))

    async def _finish(self, outcome: str | None, note: str = "") -> None:
        active = self.active
        if active is None:
            return
        for task in (active.tick_task, active.photo_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if active.photo_jobs:
            await asyncio.wait(active.photo_jobs, timeout=5.0)
            for job in active.photo_jobs:
                job.cancel()
        self._flush(active)
        active.writer.consolidate()
        for w in active.raw_writers.values():
            w.consolidate()
        active.meta.outcome = outcome  # type: ignore[assignment]
        if note:
            active.meta.notes = "; ".join(x for x in (active.meta.notes, note) if x)
        self._write_meta()
        self.active = None
        self.finished.append(active.run_dir)
        log.info("run %s ended (%s), %d rows", active.meta.run_id, outcome, active.n_rows)

    def _write_meta(self) -> None:
        active = self.active
        assert active is not None
        path = active.run_dir / META_FILE
        if path.exists():
            try:
                on_disk = read_meta(active.run_dir)
                for name in SERVICE_FIELDS:
                    setattr(active.meta, name, getattr(on_disk, name))
            except ValueError:
                log.warning("existing meta.json in %s unreadable; overwriting", active.run_dir)
        write_meta(active.run_dir, active.meta)

    # ------------------------------------------------------------------ rows
    async def _tick_loop(self, active: _ActiveRun) -> None:
        k = 0
        while True:
            target = active.t0 + k * self.dt
            delay = target - self.clock()
            if delay > 0:
                await asyncio.sleep(delay)
            row = self.sampler.tick(target)
            row["t"] = round(k * self.dt, 6)
            active.rows.append(row)
            active.n_rows += 1
            k += 1
            if self.clock() - active.last_flush >= self.flush_interval_s:
                self._flush(active)
                self._write_meta()

    def _flush(self, active: _ActiveRun) -> None:
        if active.rows:
            frame = pd.DataFrame(active.rows, columns=["t", *self.sampler.columns])
            active.writer.write(frame)
            active.rows = []
        if self.sampler.raw is not None:
            for name, frame in self.sampler.raw.drain().items():
                active.raw_writers[name].write(frame)
        active.last_flush = self.clock()

    # ------------------------------------------------------------------ photos
    def _photo_soon(self) -> None:
        active = self.active
        if active is None or self.snapshot is None:
            return
        t_s = round(self.clock() - active.t0, 3)
        job = asyncio.create_task(self._take_photo(active, t_s))
        active.photo_jobs.add(job)
        job.add_done_callback(active.photo_jobs.discard)

    async def _photo_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._photo_soon()

    async def _take_photo(self, active: _ActiveRun, t_s: float) -> None:
        assert self.snapshot is not None
        try:
            data = await self.snapshot()
        except Exception as exc:  # camera trouble must never stop logging
            log.warning("snapshot failed: %s", exc)
            return
        async with active.photo_lock:
            n = len(active.meta.photos) + 1
            rel = f"{PHOTO_DIR}/{n:04d}.jpg"  # ASSUMPTION: webcam snapshots are JPEG
            path = active.run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            active.meta.photos.append(PhotoRecord(t_s=t_s, path=rel))
            active.meta.photos.sort(key=lambda p: p.t_s)
