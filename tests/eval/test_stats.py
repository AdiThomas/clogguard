import math

import pytest

from clogguard.eval import stats


@pytest.mark.parametrize(("x", "n", "ok"), [(26, 30, True), (25, 30, False), (18, 20, True),
                                            (17, 20, False), (21, 30, False)])
def test_lower_bound_vs_70(x: int, n: int, ok: bool) -> None:
    assert (stats.clopper_pearson_lower(x, n) >= 0.70) is ok


def test_upper_bounds_vs_10() -> None:
    assert stats.clopper_pearson_upper(0, 30) < 0.10
    assert stats.clopper_pearson_upper(1, 30) > 0.10
    assert stats.clopper_pearson_upper(1, 50) < 0.10
    assert stats.clopper_pearson_upper(2, 50) > 0.10


def test_known_values_and_edges() -> None:
    assert stats.clopper_pearson_upper(0, 30) == pytest.approx(1 - 0.05 ** (1 / 30))
    assert stats.clopper_pearson_lower(30, 30) == pytest.approx(0.05 ** (1 / 30))
    assert stats.clopper_pearson_lower(0, 10) == 0.0
    assert stats.clopper_pearson_upper(10, 10) == 1.0
    lo, hi = stats.clopper_pearson_interval(5, 10)
    assert lo < 0.5 < hi
    with pytest.raises(ValueError):
        stats.clopper_pearson_lower(5, 4)


def test_min_successes() -> None:
    assert stats.min_successes(30, 0.70) == 26
    assert stats.min_successes(20, 0.70) == 18
    assert stats.min_successes(3, 0.99) is None
    assert stats.max_failures(30, 0.10) == 0
    assert stats.max_failures(50, 0.10) == 1
    assert stats.max_failures(10, 0.10) is None


def test_wilson() -> None:
    lo, hi = stats.wilson_interval(26, 30)
    assert 0.6 < lo < 0.75 < 0.9 < hi < 1.0
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)
    assert stats.wilson_interval(0, 10)[0] == 0.0


def test_mann_whitney_and_bootstrap() -> None:
    a, b = [1.0, 2, 3, 2, 1, 2], [10.0, 12, 9, 11, 13, 10]
    assert stats.mann_whitney(a, b, "less").p_value < 0.01
    assert math.isnan(stats.mann_whitney([], b).p_value)
    est, lo, hi = stats.bootstrap_diff_ci(b, a, "mean", seed=1)
    assert lo < est < hi and lo > 0
    assert stats.bootstrap_diff_ci(b, a, "mean", seed=1) == (est, lo, hi)  # seeded
    m = stats.bootstrap_diff_ci(b, a, "median", seed=1)
    assert m[0] == pytest.approx(8.5)
    c = stats.bootstrap_diff_ci(b, a, lambda v: float(v.max()), n_boot=200)
    assert c[0] == 10
    with pytest.raises(ValueError):
        stats.bootstrap_diff_ci(a, b, "mode")
    assert math.isnan(stats.bootstrap_diff_ci([], b)[0])
    red, rlo, rhi = stats.bootstrap_ratio_reduction_ci(a, b, seed=2)
    assert rlo < red < rhi and red > 0.8
    assert math.isnan(stats.bootstrap_ratio_reduction_ci([], b)[0])
