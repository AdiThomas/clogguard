"""Statistics for Table 2: exact binomial bounds, Wilson interval, rank test, bootstrap CIs."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps


def _check(x: int, n: int) -> None:
    if n < 0 or x < 0 or x > n:
        raise ValueError(f"need 0 <= x <= n, got x={x}, n={n}")


def clopper_pearson_lower(x: int, n: int, conf: float = 0.95) -> float:
    """Exact one-sided lower confidence bound on a proportion (Clopper-Pearson)."""
    _check(x, n)
    if n == 0 or x == 0:
        return 0.0
    return float(sps.beta.ppf(1.0 - conf, x, n - x + 1))


def clopper_pearson_upper(x: int, n: int, conf: float = 0.95) -> float:
    """Exact one-sided upper confidence bound on a proportion (Clopper-Pearson)."""
    _check(x, n)
    if n == 0 or x == n:
        return 1.0
    return float(sps.beta.ppf(conf, x + 1, n - x))


def clopper_pearson_interval(x: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact two-sided interval."""
    a = (1.0 - conf) / 2.0
    return clopper_pearson_lower(x, n, 1.0 - a), clopper_pearson_upper(x, n, 1.0 - a)


def wilson_interval(x: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Two-sided Wilson score interval, for display only (the pass rules use exact bounds)."""
    _check(x, n)
    if n == 0:
        return 0.0, 1.0
    z = float(sps.norm.ppf(1.0 - (1.0 - conf) / 2.0))
    p = x / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def min_successes(n: int, target: float, conf: float = 0.95) -> int | None:
    """Smallest x out of n whose one-sided lower bound is >= target. None if none does."""
    for x in range(n + 1):
        if clopper_pearson_lower(x, n, conf) >= target:
            return x
    return None


def max_failures(n: int, limit: float, conf: float = 0.95) -> int | None:
    """Largest x out of n whose one-sided upper bound is < limit. None if even 0 fails."""
    best: int | None = None
    for x in range(n + 1):
        if clopper_pearson_upper(x, n, conf) < limit:
            best = x
        else:
            break
    return best


@dataclass(frozen=True)
class UTest:
    statistic: float
    p_value: float
    alternative: str
    n_a: int
    n_b: int


def mann_whitney(
    a: Sequence[float], b: Sequence[float], alternative: str = "less"
) -> UTest:
    """Mann-Whitney U test of a against b (``less``: a tends to be smaller than b)."""
    a_arr, b_arr = np.asarray(a, float), np.asarray(b, float)
    if len(a_arr) == 0 or len(b_arr) == 0:
        return UTest(float("nan"), float("nan"), alternative, len(a_arr), len(b_arr))
    res = sps.mannwhitneyu(a_arr, b_arr, alternative=alternative)
    return UTest(float(res.statistic), float(res.pvalue), alternative, len(a_arr), len(b_arr))


def bootstrap_diff_ci(
    a: Sequence[float],
    b: Sequence[float],
    stat: str | Callable[[np.ndarray], float] = "mean",
    conf: float = 0.95,
    n_boot: int = 5000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile bootstrap CI for stat(a) - stat(b). Returns (estimate, lo, hi). Seeded."""
    fn: Callable[[np.ndarray], float]
    if stat == "mean":
        fn = np.mean
    elif stat == "median":
        fn = np.median
    elif callable(stat):
        fn = stat
    else:
        raise ValueError(f"unknown stat {stat!r}")
    a_arr, b_arr = np.asarray(a, float), np.asarray(b, float)
    if len(a_arr) == 0 or len(b_arr) == 0:
        nan = float("nan")
        return nan, nan, nan
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(a_arr), size=(n_boot, len(a_arr)))
    ib = rng.integers(0, len(b_arr), size=(n_boot, len(b_arr)))
    if fn is np.mean:
        diffs = a_arr[ia].mean(axis=1) - b_arr[ib].mean(axis=1)
    elif fn is np.median:
        diffs = np.median(a_arr[ia], axis=1) - np.median(b_arr[ib], axis=1)
    else:
        diffs = np.array([fn(a_arr[i]) - fn(b_arr[j]) for i, j in zip(ia, ib, strict=True)])
    alpha = (1.0 - conf) / 2.0
    est = float(fn(a_arr) - fn(b_arr))
    return est, float(np.quantile(diffs, alpha)), float(np.quantile(diffs, 1 - alpha))


def bootstrap_ratio_reduction_ci(
    system: Sequence[float],
    baseline: Sequence[float],
    conf: float = 0.95,
    n_boot: int = 5000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile bootstrap CI for 1 - mean(system)/mean(baseline). Returns (est, lo, hi)."""
    s, b = np.asarray(system, float), np.asarray(baseline, float)
    if len(s) == 0 or len(b) == 0 or b.mean() == 0:
        nan = float("nan")
        return nan, nan, nan
    rng = np.random.default_rng(seed)
    ms = s[rng.integers(0, len(s), size=(n_boot, len(s)))].mean(axis=1)
    mb = b[rng.integers(0, len(b), size=(n_boot, len(b)))].mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        red = 1.0 - ms / mb
    red = red[np.isfinite(red)]
    alpha = (1.0 - conf) / 2.0
    return (
        float(1.0 - s.mean() / b.mean()),
        float(np.quantile(red, alpha)),
        float(np.quantile(red, 1 - alpha)),
    )
