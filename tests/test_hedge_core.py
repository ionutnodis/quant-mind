"""Golden-value tests for the hedge/construction core: drawdown, leverage
headroom (drawdown-budget), and the diversification ratio."""

import numpy as np
import pandas as pd
import pytest

from quantmind.hedge.core import diversification_ratio, leverage_headroom, max_drawdown
from quantmind.risk.returns import InsufficientDataError


def test_max_drawdown_golden():
    # cum = [1.10, 0.88, 0.924]; peak 1.10; trough 0.88 -> MDD = 1 - 0.88/1.10 = 0.20
    r = pd.Series([0.10, -0.20, 0.05])
    assert max_drawdown(r) == pytest.approx(0.20)


def test_max_drawdown_requires_two_observations():
    with pytest.raises(InsufficientDataError):
        max_drawdown(pd.Series([0.01]))


def test_leverage_headroom_scales_to_the_drawdown_budget():
    r = pd.Series([0.10, -0.20, 0.05])  # MDD = 0.20
    assert leverage_headroom(r, drawdown_budget=0.10) == pytest.approx(0.5)  # de-lever
    assert leverage_headroom(r, drawdown_budget=0.40) == pytest.approx(2.0)  # room to add


def test_leverage_headroom_undefined_without_a_drawdown():
    # A monotonically rising series never draws down -> headroom undefined.
    with pytest.raises(ValueError):
        leverage_headroom(pd.Series([0.01, 0.02, 0.03]), drawdown_budget=0.10)


def test_leverage_headroom_rejects_nonpositive_budget():
    with pytest.raises(ValueError):
        leverage_headroom(pd.Series([0.10, -0.20]), drawdown_budget=0.0)


def test_diversification_ratio_orthogonal_vs_collinear():
    # A and B: equal vol, exactly uncorrelated (orthogonal sign pattern).
    a = pd.Series([0.01, -0.01, 0.01, -0.01])
    b = pd.Series([0.01, 0.01, -0.01, -0.01])
    df = pd.DataFrame({"A": a, "B": b})
    w = np.array([0.5, 0.5])
    # equal-weight, equal-vol, uncorrelated -> DR = sqrt(2)
    assert diversification_ratio(df, w) == pytest.approx(np.sqrt(2.0))

    # perfectly collinear -> no diversification -> DR = 1
    df2 = pd.DataFrame({"A": a, "B": a})
    assert diversification_ratio(df2, w) == pytest.approx(1.0)


def test_diversification_ratio_requires_two_instruments():
    with pytest.raises(InsufficientDataError):
        diversification_ratio(pd.DataFrame({"A": [0.01, -0.01, 0.02]}), np.array([1.0]))
