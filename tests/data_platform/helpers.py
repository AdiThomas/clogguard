from datetime import UTC, datetime

import numpy as np
import pandas as pd

from clogguard.core.schema import InductionSpec, PhotoRecord, Run, RunMetadata


def make_run(
    run_id: str = "run_a",
    n: int = 50,
    data_source: str = "real",
    induction: str = "none",
    purpose: str = "baseline",
    **meta_kw,
) -> Run:
    t = np.round(np.arange(n) / 10.0, 6)
    frame = pd.DataFrame(
        {"t": t, "extruder_power": np.linspace(0.3, 0.5, n), "extruder_target_c": 210.0}
    )
    meta = RunMetadata(
        run_id=run_id,
        data_source=data_source,
        started_at=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
        purpose=purpose,
        induction=InductionSpec(method=induction),
        channels=["extruder_power", "extruder_target_c"],
        **meta_kw,
    )
    return Run(meta=meta, frame=frame)


def photos(times: list[float]) -> list[PhotoRecord]:
    return [PhotoRecord(t_s=t, path=f"photos/{i:04d}.jpg") for i, t in enumerate(times, 1)]
