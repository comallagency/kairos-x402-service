"""5 reference-verified cases per route. [2,4,4,4,5,5,7,9] is the classic
mean/median worked example (Wikipedia's "Mean" article). Perfect linear
data gives a Pearson r and regression R-squared of exactly +-1 or 1.0 by
the mathematical DEFINITION of those statistics, independent of any
particular implementation - the strongest possible reference. All values
cross-checked with a standalone script against Python's own stdlib
`statistics` module before being hardcoded."""

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.stats import (
    CorrelationInput, PercentileInput, RegressionInput, SummaryInput,
    compute_correlation, compute_percentile, compute_regression, compute_summary,
)


# ---------------------------------------------------------------- stats/summary
def test_summary_classic_wikipedia_mean_example():
    r = compute_summary(SummaryInput(data=[2, 4, 4, 4, 5, 5, 7, 9]))
    assert r.count == 8
    assert r.mean == 5.0
    assert r.median == 4.5
    assert r.min == 2.0 and r.max == 9.0


def test_summary_stdev_matches_hand_formula():
    r = compute_summary(SummaryInput(data=[2, 4, 4, 4, 5, 5, 7, 9]))
    assert r.stdev == pytest.approx(2.138090, abs=1e-5)


def test_summary_single_value_no_stdev():
    r = compute_summary(SummaryInput(data=[5.0]))
    assert r.stdev is None and r.variance is None
    assert r.mean == 5.0 and r.median == 5.0


def test_summary_two_values():
    r = compute_summary(SummaryInput(data=[1.0, 3.0]))
    assert r.mean == 2.0 and r.median == 2.0


def test_summary_negative_numbers():
    r = compute_summary(SummaryInput(data=[-5, -3, -1, 1, 3]))
    assert r.mean == -1.0 and r.median == -1.0


# ------------------------------------------------------------ stats/correlation
def test_correlation_perfect_positive():
    r = compute_correlation(CorrelationInput(x=[1, 2, 3, 4, 5], y=[2, 4, 6, 8, 10]))
    assert r.pearson_r == 1.0


def test_correlation_perfect_negative():
    r = compute_correlation(CorrelationInput(x=[1, 2, 3, 4, 5], y=[10, 8, 6, 4, 2]))
    assert r.pearson_r == -1.0


def test_correlation_no_linear_relationship():
    # Symmetric parabola around the mean x - zero linear correlation by
    # construction (textbook example of correlation missing a real
    # non-linear relationship).
    r = compute_correlation(CorrelationInput(x=[-2, -1, 0, 1, 2], y=[4, 1, 0, 1, 4]))
    assert r.pearson_r == pytest.approx(0.0, abs=1e-9)


def test_correlation_length_mismatch_rejected():
    with pytest.raises(ComputeError):
        compute_correlation(CorrelationInput(x=[1, 2, 3], y=[1, 2]))


def test_correlation_constant_series_undefined():
    with pytest.raises(ComputeError):
        compute_correlation(CorrelationInput(x=[1, 1, 1], y=[1, 2, 3]))


# -------------------------------------------------------------- stats/regression
def test_regression_perfect_line():
    r = compute_regression(RegressionInput(x=[1, 2, 3, 4, 5], y=[2, 4, 6, 8, 10]))
    assert r.slope == 2.0 and r.intercept == 0.0 and r.r_squared == 1.0


def test_regression_with_intercept():
    # y = 3x + 1 exactly.
    r = compute_regression(RegressionInput(x=[0, 1, 2, 3], y=[1, 4, 7, 10]))
    assert r.slope == 3.0 and r.intercept == 1.0 and r.r_squared == 1.0


def test_regression_negative_slope():
    r = compute_regression(RegressionInput(x=[1, 2, 3], y=[9, 6, 3]))
    assert r.slope == -3.0


def test_regression_degenerate_x_rejected():
    with pytest.raises(ComputeError):
        compute_regression(RegressionInput(x=[5, 5, 5], y=[1, 2, 3]))


def test_regression_length_mismatch_rejected():
    with pytest.raises(ComputeError):
        compute_regression(RegressionInput(x=[1, 2, 3], y=[1, 2]))


# -------------------------------------------------------------- stats/percentile
def test_percentile_median_of_1_to_10():
    r = compute_percentile(PercentileInput(data=list(range(1, 11)), percentile=50))
    assert r.value == 5.5


def test_percentile_0_is_min():
    r = compute_percentile(PercentileInput(data=list(range(1, 11)), percentile=0))
    assert r.value == 1.0


def test_percentile_100_is_max():
    r = compute_percentile(PercentileInput(data=list(range(1, 11)), percentile=100))
    assert r.value == 10.0


def test_percentile_single_value():
    r = compute_percentile(PercentileInput(data=[42.0], percentile=50))
    assert r.value == 42.0


def test_percentile_25th_hand_computed():
    # rank = 0.25 * 9 = 2.25 -> between index 2 (value 3) and 3 (value 4),
    # interpolated: 3 + 0.25*(4-3) = 3.25.
    r = compute_percentile(PercentileInput(data=list(range(1, 11)), percentile=25))
    assert r.value == 3.25
