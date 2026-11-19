"""Write small synthetic run directories by hand with the core models."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from clogguard.core.commands import Notify, Pause, SetSpeedFactor
from clogguard.core.events import Assessment, AuditRecord, CommandResult
from clogguard.core.schema import DefectRating, InductionSpec, RunMetadata

T0 = datetime(2027, 3, 1, tzinfo=UTC)


def make_run(
    root: Path,
    run_id: str,
    *,
    purpose: str = "other",
    clog: bool = False,
    mode: str = "off",
    source: str = "simulated",
    recoverable: bool | None = None,
    onset: float | None = None,
    rated: bool = True,
    alert: float | None = None,
    waste: float | None = None,
    part_mass: float | None = None,
    reference_mass: float | None = None,
    commands: list[tuple[float, str]] | None = None,  # (t, kind) accepted
    assess_dt: float | None = None,
    duration: float = 60.0,
    ack_latency: float = 0.3,
    frame: bool = True,
    **meta_kw: Any,
) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    induction = (
        InductionSpec(method="temp_step", severity="-20C", recoverable=recoverable)
        if clog
        else InductionSpec(method="none")
    )
    ratings = []
    if rated:
        ratings = [DefectRating(rater="r1", t_s=onset), DefectRating(rater="r2", t_s=None)]
    meta = RunMetadata(
        run_id=run_id,
        data_source=source,  # type: ignore[arg-type]
        started_at=T0,
        purpose=purpose,  # type: ignore[arg-type]
        induction=induction,
        manipulated_variables=["extruder_target_c"] if clog else [],
        service_active=mode != "off",
        service_mode=mode,  # type: ignore[arg-type]
        alert_time_s=alert,
        defect_ratings=ratings,
        wasted_filament_g=waste,
        part_mass_g=part_mass,
        reference_mass_g=reference_mass,
        source_citation="test fixture" if source == "open_dataset" else "",
        **meta_kw,
    )
    (d / "meta.json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    if frame:
        t = np.arange(0, duration, 0.1)
        pd.DataFrame(
            {"t": t, "extruder_power": 0.4 + 0.01 * np.sin(t), "volumetric_flow_mm3_s": 8.0}
        ).to_parquet(d / "timeseries.parquet")
    records: list[AuditRecord] = []
    seq = 0

    def add(ev: Any) -> None:
        nonlocal seq
        records.append(AuditRecord(seq=seq, run_id=run_id, event=ev))
        seq += 1

    if assess_dt is not None:
        for ts in np.arange(0, duration, assess_dt):
            add(Assessment(t_s=float(ts), wall_time=T0, state="normal", p_clog=0.1))
    for ts, kind in commands or []:
        cmd: Any
        if kind == "notify":
            cmd = Notify(level="warning", message="clog developing")
        elif kind == "pause":
            cmd = Pause(reason="clog")
        else:
            cmd = SetSpeedFactor(percent=80)
        add(
            CommandResult(
                t_s=ts, wall_time=T0, command=cmd, accepted=True, decision_t_s=ts,
                sent_at_s=ts + 0.05, acked_at_s=ts + ack_latency,
            )
        )
    if records:
        (d / "audit.jsonl").write_text(
            "\n".join(r.model_dump_json() for r in records) + "\n", encoding="utf-8"
        )
    return d
