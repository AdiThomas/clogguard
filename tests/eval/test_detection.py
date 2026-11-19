from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from clogguard.core.schema import DefectRating, InductionSpec, RunMetadata
from clogguard.eval.detection import alert_time, evaluate_alerts, sweep

T0 = datetime(2027, 1, 1, tzinfo=UTC)


def _meta(rid: str, clog: bool, onset: float | None = None) -> RunMetadata:
    return RunMetadata(
        run_id=rid, data_source="simulated", started_at=T0,
        induction=InductionSpec(method="simulated" if clog else "none"),
        defect_ratings=[DefectRating(rater="a", t_s=onset)] if clog else [],
    )


def _ramp(start: float, end: float = 1800.0) -> pd.DataFrame:
    t = np.arange(0, end, 1.0)
    return pd.DataFrame({"t": t, "p_clog": np.clip((t - start) / 600.0, 0, 1)})


def test_alert_time_persistence() -> None:
    df = pd.DataFrame({"t": [0, 1, 2, 3, 4, 5, 6.0], "p_clog": [0, .9, 0, .9, .9, .9, np.nan]})
    assert alert_time(df, 0.5) == 1.0
    assert alert_time(df, 0.5, persist_s=2) == 5.0
    assert alert_time(df, 0.5, persist_s=3) is None
    assert alert_time(df.iloc[:0], 0.5) is None


def test_evaluate_and_sweep() -> None:
    scores = {"c1": _ramp(300), "c2": _ramp(600), "n1": _ramp(10_000),
              "n2": pd.DataFrame({"t": [0.0, 1.0], "p_clog": [0.6, 0.2]}),
              "c3": _ramp(10_000)}
    metas = {"c1": _meta("c1", True, 1200.0), "c2": _meta("c2", True, 1200.0),
             "c3": _meta("c3", True, 1500.0), "n1": _meta("n1", False),
             "n2": _meta("n2", False)}
    res = evaluate_alerts(scores, metas, threshold=0.5)
    pr = res.per_run.set_index("run_id")
    assert pr.loc["c1", "alert_t_s"] == 600.0 and pr.loc["c1", "lead_s"] == 600.0
    assert pr.loc["c2", "lead_s"] == 300.0
    assert pr.loc["c3", "lead_s"] == 0.0 and bool(pr.loc["c3", "miss"])
    assert bool(pr.loc["n2", "false_alert"]) and not bool(pr.loc["n1", "false_alert"])
    s = res.summary()
    assert s["n_clog"] == 3 and s["false_alerts"] == 1 and s["missed"] == 1
    assert s["median_lead_min"] == pytest.approx(5.0)
    assert s["data_source"] == "simulated"
    table = sweep(scores, metas, thresholds=[0.3, 0.5, 0.7], persist_s=[0, 5])
    assert len(table) == 6
    t0 = table[table["persist_s"] == 0].set_index("threshold")
    assert t0.loc[0.3, "false_alerts"] == 1 and t0.loc[0.7, "false_alerts"] == 0
    assert t0.loc[0.3, "median_lead_min"] > t0.loc[0.7, "median_lead_min"]
    t5 = table[table["persist_s"] == 5].set_index("threshold")
    assert t5.loc[0.3, "false_alerts"] == 0  # persistence suppresses the blip
    with pytest.raises(KeyError):
        evaluate_alerts({"x": _ramp(1)}, metas, 0.5)
