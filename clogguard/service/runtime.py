"""The real-time service: status in, assessments and gated commands out.

Data flow per print::

    PrinterLink.updates() -> merged status -> StatusRowBuilder (base-rate rows)
        every assess_interval_s of printer time:
        OnlineScorer.update(rows, t) (thread, time budget) -> Assessment
        -> RecoveryLadder.step -> Decision -> SafetyGate.submit -> CommandResult
        -> ladder.on_result; every event -> audit.jsonl + EventBus

Time base: ``t_s = eventtime - print_start_eventtime``. Assessment ticks are driven by the
eventtimes of incoming updates, never by the wall clock, so an accelerated mock printer whose
eventtime is simulated seconds replays correctly. The gate's ack timeout and the staleness
watchdog use the wall (monotonic) clock.

Print detection from ``print_stats.state``: entering ``printing`` with no active print starts a
print (new scorer, ladder, gate reset, run dir); ``complete``, ``cancelled``, ``error`` or
``standby`` ends it; ``paused`` keeps it (assessments continue, decisions hold). A filename
change while printing ends the old print and starts a new one.

Safety rules applied here: missing or stale data, a scorer error, or a scorer slower than its
budget produce an ``unknown`` assessment with ``data_fresh=False`` (the ladder holds) and a
``Lifecycle(data_stale)`` event. On disconnect the service holds, logs, and resumes when the
link is back. It never sends anything on shutdown: a stopped service leaves a stock printer.
This module never calls the link's RPC method; only ``clogguard.safety`` does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from clogguard.core.channels import ChannelConfig
from clogguard.core.config import ServiceConfig
from clogguard.core.events import (
    Assessment,
    AuditEvent,
    AuditRecord,
    CommandResult,
    Decision,
    Lifecycle,
)
from clogguard.core.model_api import OnlineScorer
from clogguard.safety import Notifier, PrinterLink, SafetyGate
from clogguard.service.audit import AuditWriter, find_logger_run, make_run_id, update_meta
from clogguard.service.bus import EventBus
from clogguard.service.ladder import RecoveryLadder
from clogguard.service.rows import StatusRowBuilder, subscription_objects

log = logging.getLogger(__name__)

SCORER_BUDGET_S = 0.1  # docs/plan.md: under 100 ms per inference on the Klipper host
END_STATES = frozenset({"complete", "cancelled", "error", "standby"})


class LoggingNotifier:
    """Default notifier: writes to the Python log. The dashboard subscribes to the EventBus."""

    async def notify(self, level: str, message: str) -> None:
        log.warning("NOTIFY [%s] %s", level, message)


@dataclass
class _Print:
    run_id: str
    run_dir: Path
    filename: str
    start_et: float
    scorer: OnlineScorer
    ladder: RecoveryLadder
    rows: StatusRowBuilder
    audit: AuditWriter
    next_assess_t: float = 0.0
    results: list[CommandResult] = field(default_factory=list)
    stale: bool = False
    stale_reason: str = ""
    watchdog_stale: bool = False
    scorer_future: asyncio.Future[Any] | None = None


class Service:
    def __init__(
        self,
        link: PrinterLink,
        channels: ChannelConfig,
        config: ServiceConfig,
        scorer_factory: Callable[[], OnlineScorer],
        *,
        notifier: Notifier | None = None,
        bus: EventBus[AuditRecord] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        scorer_budget_s: float = SCORER_BUDGET_S,
        poll_s: float = 0.1,
        run_id_fn: Callable[[datetime, str], str] = make_run_id,
        match_run_fn: Callable[[Path, str, datetime], tuple[str, Path] | None] = find_logger_run,
    ) -> None:
        self.link = link
        self.channels = channels
        self.config = config
        self.scorer_factory = scorer_factory
        self.bus: EventBus[AuditRecord] = bus if bus is not None else EventBus()
        self.gate = SafetyGate(
            config, link, notifier or LoggingNotifier(), clock=clock, wall_clock=wall_clock
        )
        self._clock = clock
        self._wall = wall_clock
        self._budget = scorer_budget_s
        self._poll = poll_s
        self._run_id_fn = run_id_fn
        self._match_run_fn = match_run_fn
        self._objects = subscription_objects(channels)
        self._status: dict[str, dict[str, Any]] = {}
        self._print: _Print | None = None
        self._stop = asyncio.Event()
        self._svc_seq = 0
        self._initial = False
        self._last_update_wall = clock()
        self.completed_runs: list[str] = []

    # -- public --------------------------------------------------------------------------
    @property
    def current_run_id(self) -> str | None:
        return None if self._print is None else self._print.run_id

    @property
    def current_print(self) -> _Print | None:
        return self._print

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self._emit(self._life("service_start", f"mode={self.config.mode}"))
        main = asyncio.create_task(self._main())
        stopper = asyncio.create_task(self._stop.wait())
        watchdog = asyncio.create_task(self._watchdog())
        try:
            await asyncio.wait({main, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if main.done():
                main.result()  # re-raise an unexpected crash of the main loop
        finally:
            for task in (main, stopper, watchdog):
                task.cancel()
            for task in (main, stopper, watchdog):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._emit(self._life("service_stop"))
            if self._print is not None:
                self._close_print(ended=False)

    # -- connection loop -----------------------------------------------------------------
    async def _main(self) -> None:
        while True:
            if not self.link.connected:
                await asyncio.sleep(self._poll)
                continue
            try:
                res = await self.link.subscribe(self._objects)
            except Exception as exc:
                log.warning("subscribe failed: %s", exc)
                await asyncio.sleep(self._poll)
                continue
            self._emit(self._life("printer_ready"))
            status = res.get("status", res) if isinstance(res, dict) else {}
            status = status if isinstance(status, dict) else {}
            et = res.get("eventtime") if isinstance(res, dict) else None
            self._initial = True
            if isinstance(et, int | float) and math.isfinite(et):
                await self._handle(status, float(et))
            else:
                self._merge(status)  # no eventtime yet: print detection waits for an update
            try:
                async for diff, et in self.link.updates():
                    await self._handle(diff, float(et))
            except Exception as exc:
                log.warning("update stream failed: %s", exc)
            self._on_disconnect()

    def _on_disconnect(self) -> None:
        self._emit(self._life("printer_disconnected"))
        ctx = self._print
        if ctx is not None and not ctx.stale:
            ctx.stale, ctx.stale_reason = True, "printer disconnected"
            self._emit(self._life("data_stale", "printer disconnected"))

    async def _watchdog(self) -> None:
        stale_after = self.config.detector.stale_after_s
        while True:
            await asyncio.sleep(min(0.25, stale_after / 4))
            ctx = self._print
            idle = self._clock() - self._last_update_wall
            if ctx is not None and idle > stale_after and not ctx.watchdog_stale:
                ctx.watchdog_stale = True
                if not ctx.stale:
                    ctx.stale, ctx.stale_reason = True, "no printer updates"
                    self._emit(self._life("data_stale", f"no printer updates for {idle:.1f}s"))

    # -- status handling -----------------------------------------------------------------
    def _merge(self, diff: dict[str, Any]) -> None:
        for obj, fields in diff.items():
            if isinstance(fields, dict):
                self._status[obj] = {**self._status.get(obj, {}), **fields}

    async def _handle(self, diff: dict[str, Any], eventtime: float) -> None:
        self._last_update_wall = self._clock()
        initial, self._initial = self._initial, False
        self._merge(diff)
        ps = self._status.get("print_stats", {})
        state = ps.get("state")
        filename = str(ps.get("filename") or "")
        ctx = self._print
        if ctx is not None and (state in END_STATES or (state == "printing" and
                                                       filename != ctx.filename)):
            self._emit(self._life("print_end", f"state={state}"))
            self._close_print(ended=True)
            ctx = None
        if ctx is None:
            if state != "printing":
                return
            start_et = eventtime
            duration = ps.get("print_duration")
            if initial and isinstance(duration, int | float) and math.isfinite(duration):
                # ASSUMPTION: joining a print already running, print_duration approximates the
                # time since it started (it excludes pauses).
                start_et = eventtime - float(duration)
            ctx = self._start_print(filename, start_et)
            ctx.rows.update(self._status, eventtime)
        else:
            ctx.watchdog_stale = False
            ctx.rows.update(diff, eventtime)
        t = eventtime - ctx.start_et
        if t + 1e-9 >= ctx.next_assess_t:
            interval = self.config.detector.assess_interval_s
            while ctx.next_assess_t <= t + 1e-9:
                ctx.next_assess_t += interval
            await self._tick(ctx, t)

    def _start_print(self, filename: str, start_et: float) -> _Print:
        wall = self._wall()
        runs_dir = Path(self.config.runs_dir)
        found = self._match_run_fn(runs_dir, filename, wall)
        if found is not None:
            run_id, run_dir = found
        else:
            run_id = self._run_id_fn(wall, filename)
            run_dir = runs_dir / run_id
        self.gate.reset_for_new_print()
        ctx = _Print(
            run_id=run_id,
            run_dir=run_dir,
            filename=filename,
            start_et=start_et,
            scorer=self.scorer_factory(),
            ladder=RecoveryLadder(self.config),
            rows=StatusRowBuilder(
                self.channels,
                start_eventtime=start_et,
                filament_area_mm2=self.config.material.filament_area_mm2,
                stale_after_s=self.config.detector.stale_after_s,
            ),
            audit=AuditWriter(run_dir, run_id),
        )
        self._print = ctx
        detail = f"file={filename} reused_logger_dir={found is not None}"
        self._emit(self._life("print_start", detail))
        return ctx

    def _close_print(self, *, ended: bool) -> None:
        ctx = self._print
        assert ctx is not None
        try:
            update_meta(
                ctx.run_dir,
                mode=self.config.mode,
                alert_time_s=ctx.ladder.first_warn_t_s,
                results=ctx.results,
            )
        except Exception as exc:  # never let bookkeeping crash the service
            log.warning("could not update meta.json for %s: %s", ctx.run_id, exc)
        ctx.audit.close()
        if ended:
            self.completed_runs.append(ctx.run_id)
        self._print = None

    # -- one assessment tick -------------------------------------------------------------
    async def _tick(self, ctx: _Print, t: float) -> None:
        rows = ctx.rows.take_rows(t)
        fresh, why = ctx.rows.freshness(t)
        if ctx.watchdog_stale:
            fresh, why = False, "no printer updates"
        scored, err = await self._score(ctx, rows, t)
        if scored is None:
            a = Assessment(t_s=t, wall_time=self._wall(), state="unknown", data_fresh=False)
            why = err
        else:
            ok = fresh and scored.data_fresh and scored.state != "unknown"
            if fresh and not ok:
                why = "scorer reported unknown state or stale data"
            a = scored.model_copy(update={"t_s": t, "data_fresh": ok})
        self._set_stale(ctx, not a.data_fresh, why)
        self._emit(a)
        if self._is_paused():
            d = Decision(t_s=t, wall_time=a.wall_time, ladder_level=ctx.ladder.level,
                         reason="printer paused; holding")
        else:
            d = ctx.ladder.step(a)
        self._emit(d)
        self._emit_all(ctx.ladder.drain_events())
        next_d: Decision | None = d
        while next_d is not None and next_d.command is not None:
            r = await self.gate.submit(
                next_d.command, now_s=t, decision_t_s=next_d.t_s, data_fresh=a.data_fresh
            )
            ctx.results.append(r)
            self._emit(r)
            ctx.ladder.on_result(r)
            self._emit_all(ctx.ladder.drain_events())
            next_d = ctx.ladder.take_queued(t, self._wall())
            if next_d is not None:
                self._emit(next_d)

    async def _score(
        self, ctx: _Print, rows: pd.DataFrame, t: float
    ) -> tuple[Assessment | None, str]:
        if ctx.scorer_future is not None and not ctx.scorer_future.done():
            return None, "scorer still busy after exceeding its budget"
        fut = asyncio.get_running_loop().run_in_executor(None, ctx.scorer.update, rows, t)
        fut.add_done_callback(_consume_exception)
        ctx.scorer_future = fut
        done, _ = await asyncio.wait({fut}, timeout=self._budget)
        if not done:
            return None, f"scorer exceeded {self._budget * 1000:.0f} ms budget"
        exc = fut.exception()
        if exc is not None:
            return None, f"scorer error: {type(exc).__name__}: {exc}"[:300]
        result = fut.result()
        if not isinstance(result, Assessment):
            return None, "scorer returned a non-Assessment"
        return result, ""

    def _set_stale(self, ctx: _Print, stale: bool, why: str) -> None:
        if stale and (not ctx.stale or why != ctx.stale_reason):
            ctx.stale, ctx.stale_reason = True, why
            self._emit(self._life("data_stale", why))
        elif not stale and ctx.stale:
            ctx.stale, ctx.stale_reason = False, ""
            self._emit(self._life("data_restored"))

    def _is_paused(self) -> bool:
        ps = self._status.get("print_stats", {})
        pr = self._status.get("pause_resume", {})
        return ps.get("state") == "paused" or pr.get("is_paused") is True

    # -- events --------------------------------------------------------------------------
    def _life(self, what: str, detail: str = "") -> Lifecycle:
        ctx = self._print
        t = 0.0 if ctx is None else ctx.rows.last_update_t or 0.0
        return Lifecycle(t_s=t, wall_time=self._wall(), what=what, detail=detail)  # type: ignore[arg-type]

    def _emit_all(self, events: list[Any]) -> None:
        for e in events:
            self._emit(e)

    def _emit(self, event: AuditEvent) -> None:
        ctx = self._print
        if ctx is not None:
            rec = ctx.audit.write(event)
        else:
            rec = AuditRecord(seq=self._svc_seq, run_id="", event=event)
            self._svc_seq += 1
        log.debug("event %s", rec.event.type)
        self.bus.publish(rec)


def _consume_exception(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()
