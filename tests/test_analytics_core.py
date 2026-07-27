import math

import numpy as np
import pandas as pd
import pytest

from quantmind.analytics.core import correlation, covariance, returns_matrix
from quantmind.analytics.correlation import correlation_matrix
from quantmind.risk.returns import InsufficientDataError


def _idx(n):
    return pd.bdate_range("2024-01-01", periods=n)


def test_returns_matrix_simple_and_log_hand_computed():
    # A grows exactly 10% each step; B jumps +20%, -10%, +10% (all clean ratios).
    idx = _idx(4)
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0, 133.1], "B": [50.0, 60.0, 54.0, 59.4]},
        index=idx,
    )

    simple = returns_matrix(prices, method="simple")
    assert list(simple.columns) == ["A", "B"]
    assert len(simple) == 3  # 4 prices -> 3 returns after dropping the first row
    assert list(simple.index) == list(idx[1:])
    assert simple["A"].tolist() == [pytest.approx(0.10), pytest.approx(0.10), pytest.approx(0.10)]
    assert simple["B"].tolist() == [pytest.approx(0.20), pytest.approx(-0.10), pytest.approx(0.10)]

    log = returns_matrix(prices, method="log")
    assert len(log) == 3
    assert log["A"].tolist() == [pytest.approx(math.log(1.1))] * 3
    assert log["B"].tolist() == [
        pytest.approx(math.log(1.2)),   # ln(60/50)  =  0.18232155679...
        pytest.approx(math.log(0.9)),   # ln(54/60)  = -0.10536051566...
        pytest.approx(math.log(1.1)),   # ln(59.4/54)=  0.09531017980...
    ]


def test_returns_matrix_align_intersection_equals_complete_case():
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0, 133.1], "B": [50.0, 60.0, 54.0, 59.4]},
        index=_idx(4),
    )
    pd.testing.assert_frame_equal(
        returns_matrix(prices, align="intersection"),
        returns_matrix(prices, align="complete_case"),
    )


def test_covariance_golden_perfect_correlation():
    # B = 2*A exactly, so cov(A,B)=2 var(A), var(B)=4 var(A).
    idx = _idx(4)
    returns = pd.DataFrame(
        {"A": [0.01, 0.02, 0.03, 0.04], "B": [0.02, 0.04, 0.06, 0.08]},
        index=idx,
    )
    cov = covariance(returns)
    # A deviations from mean 0.025: [-.015,-.005,.005,.015]; ss = 5e-4; ddof=1 -> /3.
    var_a = 0.0005 / 3.0
    assert cov.loc["A", "A"] == pytest.approx(var_a)
    assert cov.loc["A", "B"] == pytest.approx(2.0 * var_a)
    assert cov.loc["B", "B"] == pytest.approx(4.0 * var_a)
    assert cov.loc["A", "B"] == pytest.approx(cov.loc["B", "A"])


def test_correlation_golden_perfect_positive():
    idx = _idx(4)
    returns = pd.DataFrame(
        {"A": [0.01, 0.02, 0.03, 0.04], "B": [0.02, 0.04, 0.06, 0.08]},
        index=idx,
    )
    corr = correlation(returns)
    assert corr.loc["A", "A"] == pytest.approx(1.0)
    assert corr.loc["A", "B"] == pytest.approx(1.0)


def test_correlation_golden_perfect_negative():
    idx = _idx(4)
    a = [0.01, 0.02, 0.03, 0.04]
    returns = pd.DataFrame({"A": a, "C": [-x for x in a]}, index=idx)
    corr = correlation(returns)
    assert corr.loc["A", "C"] == pytest.approx(-1.0)


def test_correlation_delegates_to_correlation_matrix():
    # DRY contract: correlation() must produce the same frame as the shared helper.
    idx = _idx(5)
    returns = pd.DataFrame(
        {"A": [0.01, -0.02, 0.03, -0.01, 0.02], "B": [0.00, 0.01, -0.01, 0.02, 0.03]},
        index=idx,
    )
    pd.testing.assert_frame_equal(correlation(returns), correlation_matrix(returns))


def test_returns_matrix_non_overlapping_histories_raises():
    # A only has prices in the first half, B only in the second -> zero common rows.
    idx = _idx(4)
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, np.nan, np.nan], "B": [np.nan, np.nan, 50.0, 55.0]},
        index=idx,
    )
    with pytest.raises(InsufficientDataError):
        returns_matrix(prices)


def test_returns_matrix_too_few_rows_raises():
    # Two prices -> one return row -> below the 2-observation floor.
    prices = pd.DataFrame({"A": [100.0, 110.0], "B": [50.0, 55.0]}, index=_idx(2))
    with pytest.raises(InsufficientDataError):
        returns_matrix(prices)


def test_covariance_single_row_raises():
    returns = pd.DataFrame({"A": [0.01], "B": [0.02]}, index=_idx(1))
    with pytest.raises(InsufficientDataError):
        covariance(returns)


def test_correlation_single_row_raises():
    returns = pd.DataFrame({"A": [0.01], "B": [0.02]}, index=_idx(1))
    with pytest.raises(InsufficientDataError):
        correlation(returns)


def test_returns_matrix_bad_method_raises_value_error():
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0, 133.1], "B": [50.0, 60.0, 54.0, 59.4]},
        index=_idx(4),
    )
    with pytest.raises(ValueError) as exc:
        returns_matrix(prices, method="bad")
    # Must be a plain ValueError, not the InsufficientDataError subclass.
    assert not isinstance(exc.value, InsufficientDataError)


def test_returns_matrix_bad_align_raises_value_error():
    prices = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0, 133.1], "B": [50.0, 60.0, 54.0, 59.4]},
        index=_idx(4),
    )
    with pytest.raises(ValueError) as exc:
        returns_matrix(prices, align="bad")
    assert not isinstance(exc.value, InsufficientDataError)
