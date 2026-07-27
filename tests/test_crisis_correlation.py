"""Golden-value tests for crisis (tail-conditioned) correlation.

Diversification decays in a crisis: correlations rush toward one on the worst
market days. These tests pin the conditioning, the minimum-tail guard, and the
bootstrap CI's reproducibility against hand-constructed data.
"""

import numpy as np
import pandas as pd
import pytest

from quantmind.analytics.correlation import crisis_correlation
from quantmind.risk.returns import InsufficientDataError


def _frame():
    idx = pd.bdate_range("2026-01-02", periods=10)
    # Worst 3 market days (tail=0.3 -> floor(10*0.3)=3) are idx 2, 4, 7.
    market = pd.Series([0.02, 0.01, -0.05, 0.015, -0.08, 0.005, 0.01, -0.06, 0.02, 0.008], index=idx)
    # On the 3 worst days A and B are perfectly correlated (B = 2A) -> crisis rho = 1.
    # On the other 7 days they alternate signs so the full-sample corr is far from 1.
    a = pd.Series([0.01, -0.01, -0.03, 0.02, -0.05, 0.01, -0.02, -0.02, 0.03, -0.01], index=idx)
    b = pd.Series([-0.01, 0.02, -0.06, -0.02, -0.10, -0.03, 0.04, -0.04, -0.05, 0.02], index=idx)
    returns = pd.DataFrame({"A": a, "B": b})
    return returns, market


def test_crisis_correlation_conditions_on_worst_market_days():
    returns, market = _frame()
    res = crisis_correlation(returns, market, tail=0.3, min_tail=3, n_boot=200, seed=7)
    # 3 worst days conditioned on.
    assert res.tail_n == 3
    # On those days B = 2A exactly -> crisis correlation is 1.
    assert res.crisis_corr.loc["A", "B"] == pytest.approx(1.0)
    assert res.crisis_mean_corr == pytest.approx(1.0)
    # Full-sample correlation is materially lower (diversification decay is real).
    assert res.normal_mean_corr < 0.95


def test_crisis_correlation_min_tail_guard():
    returns, market = _frame()
    # tail=0.1 -> floor(10*0.1)=1 worst day; below min_tail -> explicit error.
    with pytest.raises(InsufficientDataError):
        crisis_correlation(returns, market, tail=0.1, min_tail=5)


def test_crisis_correlation_bootstrap_ci_is_reproducible_and_brackets_point():
    returns, market = _frame()
    a = crisis_correlation(returns, market, tail=0.3, min_tail=3, n_boot=200, seed=11)
    b = crisis_correlation(returns, market, tail=0.3, min_tail=3, n_boot=200, seed=11)
    assert a.crisis_mean_corr_ci == b.crisis_mean_corr_ci  # same seed -> identical
    lo, hi = a.crisis_mean_corr_ci
    assert -1.0 <= lo <= hi <= 1.0


def test_crisis_correlation_carries_range_restriction_caveat():
    returns, market = _frame()
    res = crisis_correlation(returns, market, tail=0.3, min_tail=3, n_boot=50, seed=1)
    assert "range-restriction" in res.caveat.lower()
