"""Golden tests for quantmind.exposure.sensitivity (wave-3B Macro book-aware).

Pure math only (Engineering Constraint 2): typed shock in -> dollar response +
CI out, and regime-conditional return stats. Hand-computed values throughout
(TDD law: risk math tests against hand-computed/golden values).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quantmind.exposure.sensitivity import (
    Shock,
    UnsupportedShockError,
    book_shock_sensitivity,
    rate_shock,
    regime_conditional_returns,
    return_shock,
    shock_factor,
    vol_shock,
)
from quantmind.risk.factors import factor_regression
from quantmind.risk.returns import InsufficientDataError


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(end="2026-07-24", periods=n)


# --- shock constructors ----------------------------------------------------


def test_rate_shock_defaults_and_label():
    s = rate_shock("US10Y")
    assert s == Shock(driver="US10Y", kind="rate_bp", size=10.0, label="+10bp")


def test_return_shock_defaults_and_label():
    s = return_shock("XLK")
    assert s.kind == "return"
    assert s.size == pytest.approx(0.01)
    assert s.label == "+1%"


def test_vol_shock_defaults_and_label():
    s = vol_shock("VIX")
    assert s.kind == "vol_points"
    assert s.size == pytest.approx(5.0)
    assert s.label == "+5 vol pts"


def test_negative_shock_label_carries_sign():
    assert return_shock("XLK", size=-0.01).label == "-1%"
    assert rate_shock("US10Y", size_bp=-25.0).label == "-25bp"


# --- shock_factor: per-kind factor construction ----------------------------


def test_shock_factor_rate_bp_is_level_diff_in_basis_points():
    levels = pd.Series([0.040, 0.041, 0.0405], index=_idx(3))
    f = shock_factor(levels, "rate_bp")
    assert f.tolist() == pytest.approx([10.0, -5.0])


def test_shock_factor_return_is_pct_change():
    prices = pd.Series([100.0, 110.0], index=_idx(2))
    f = shock_factor(prices, "return")
    assert f.tolist() == pytest.approx([0.10])


def test_shock_factor_vol_points_is_level_diff():
    vix = pd.Series([15.0, 17.5, 16.5], index=_idx(3))
    f = shock_factor(vix, "vol_points")
    assert f.tolist() == pytest.approx([2.5, -1.0])


def test_shock_factor_unknown_kind_raises():
    with pytest.raises(UnsupportedShockError):
        shock_factor(pd.Series([1.0, 2.0], index=_idx(2)), "spread_bp")


# --- book_shock_sensitivity: golden dollar response ------------------------


def test_exact_linear_book_gives_hand_computed_dollar_response():
    # Book return is EXACTLY 0.0002 x (daily bp move): a +10bp shock on a
    # $250,000 book must respond 0.0002 * 10 * 250000 = +$500, with ~zero SE
    # (perfect fit -> zero residuals -> zero HAC variance).
    n = 40
    factor = pd.Series(np.tile([1.0, -1.0, 2.0, -2.0], n // 4), index=_idx(n), name="US10Y")
    y = 0.0002 * factor
    est = book_shock_sensitivity(y, factor, rate_shock("US10Y"), book_gross=250_000.0)
    assert est.driver == "US10Y"
    assert est.shock_label == "+10bp"
    assert est.beta == pytest.approx(0.0002, abs=1e-12)
    assert est.dollar_response == pytest.approx(500.0, abs=1e-6)
    assert est.se == pytest.approx(0.0, abs=1e-6)
    assert est.ci[0] == pytest.approx(500.0, abs=1e-4)
    assert est.ci[1] == pytest.approx(500.0, abs=1e-4)
    assert est.n_obs == n


def test_dollar_response_is_linear_transform_of_regression_beta():
    # Consistency with factor_regression: dollar = beta * size * gross,
    # se = beta_se * |size * gross|, ci = sorted(beta_ci * size * gross).
    rng = np.random.default_rng(3)
    n = 120
    factor = pd.Series(rng.normal(0.0, 1.0, n), index=_idx(n), name="US10Y")
    y = 0.0003 * factor + pd.Series(rng.normal(0.0, 0.002, n), index=_idx(n))
    shock = rate_shock("US10Y", size_bp=10.0)
    gross = 100_000.0

    est = book_shock_sensitivity(y, factor, shock, book_gross=gross)
    ref = factor_regression(y, {"US10Y": factor})
    scale = 10.0 * gross
    assert est.beta == pytest.approx(ref.betas["US10Y"])
    assert est.dollar_response == pytest.approx(ref.betas["US10Y"] * scale)
    assert est.se == pytest.approx(ref.beta_se["US10Y"] * abs(scale))
    lo, hi = sorted((ref.beta_ci["US10Y"][0] * scale, ref.beta_ci["US10Y"][1] * scale))
    assert est.ci == (pytest.approx(lo), pytest.approx(hi))
    assert est.hac_lags == ref.hac_lags


def test_negative_shock_size_keeps_ci_ordered():
    rng = np.random.default_rng(5)
    n = 80
    factor = pd.Series(rng.normal(0.0, 0.01, n), index=_idx(n), name="XLK")
    y = 0.8 * factor + pd.Series(rng.normal(0.0, 0.001, n), index=_idx(n))
    est = book_shock_sensitivity(y, factor, return_shock("XLK", size=-0.01), book_gross=50_000.0)
    assert est.ci[0] <= est.dollar_response <= est.ci[1]


def test_insufficient_overlap_raises_insufficient_data():
    factor = pd.Series([0.01, -0.01] * 5, index=_idx(10), name="XLK")
    y = pd.Series([0.005, -0.005] * 5, index=_idx(10))
    with pytest.raises(InsufficientDataError):
        book_shock_sensitivity(y, factor, return_shock("XLK"), book_gross=1_000.0)


def test_nonpositive_gross_raises_value_error():
    n = 40
    factor = pd.Series(np.tile([1.0, -1.0], n // 2), index=_idx(n), name="US10Y")
    y = 0.0002 * factor
    with pytest.raises(ValueError):
        book_shock_sensitivity(y, factor, rate_shock("US10Y"), book_gross=0.0)


# --- regime_conditional_returns: golden bucketing --------------------------


def _nine_day_case():
    idx = _idx(9)
    returns = pd.DataFrame(
        {
            "A": [0.01 * k for k in range(1, 10)],  # 1%..9%
            "B": [-0.01 * k for k in range(1, 10)],
        },
        index=idx,
    )
    regime = pd.Series(np.arange(1.0, 10.0), index=idx)  # 1..9 -> clean terciles
    return returns, regime


def test_regime_terciles_hand_computed_means_and_ses():
    returns, regime = _nine_day_case()
    buckets = regime_conditional_returns(returns, regime, n_buckets=3)
    assert [b.bucket for b in buckets] == ["low", "mid", "high"]
    low, mid, high = buckets

    assert low.n_days == 3 and mid.n_days == 3 and high.n_days == 3
    assert low.lo == pytest.approx(1.0) and low.hi == pytest.approx(3.0)
    assert high.lo == pytest.approx(7.0) and high.hi == pytest.approx(9.0)

    # A's low-regime days are 1%,2%,3%: mean 2%, se = std(ddof=1)/sqrt(3).
    assert low.mean_daily["A"] == pytest.approx(0.02)
    assert low.se_daily["A"] == pytest.approx(0.01 / math.sqrt(3))
    assert mid.mean_daily["A"] == pytest.approx(0.05)
    assert high.mean_daily["A"] == pytest.approx(0.08)
    # B is the mirror image.
    assert high.mean_daily["B"] == pytest.approx(-0.08)


def test_regime_alignment_is_inner_join_on_dates():
    returns, regime = _nine_day_case()
    buckets = regime_conditional_returns(returns, regime.iloc[:6], n_buckets=3)
    assert sum(b.n_days for b in buckets) == 6


def test_regime_single_obs_bucket_has_nan_se():
    idx = _idx(3)
    returns = pd.DataFrame({"A": [0.01, 0.02, 0.03]}, index=idx)
    regime = pd.Series([1.0, 2.0, 3.0], index=idx)
    buckets = regime_conditional_returns(returns, regime, n_buckets=3)
    assert buckets[0].n_days == 1
    assert buckets[0].mean_daily["A"] == pytest.approx(0.01)
    assert math.isnan(buckets[0].se_daily["A"])


def test_regime_constant_variable_raises():
    returns, regime = _nine_day_case()
    with pytest.raises(InsufficientDataError):
        regime_conditional_returns(returns, pd.Series(5.0, index=returns.index), n_buckets=3)


def test_regime_too_few_obs_raises():
    idx = _idx(2)
    returns = pd.DataFrame({"A": [0.01, 0.02]}, index=idx)
    regime = pd.Series([1.0, 2.0], index=idx)
    with pytest.raises(InsufficientDataError):
        regime_conditional_returns(returns, regime, n_buckets=3)
