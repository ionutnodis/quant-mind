"""CAPM excess-return regression: alpha is Jensen's alpha, not raw-return drift."""

import numpy as np
import pandas as pd
import pytest

from quantmind.risk.returns import rolling_alpha, rolling_beta


def _bench(n=400, seed=21, scale=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(rng.normal(0.0, scale, n), index=idx)


RF_DAILY = 0.0002  # ~5% annualized
BETA = 1.5
DAILY_ALPHA = 0.001


def _asset(bench, rf):
    # Exact CAPM construction: r_a = r_f + beta * (r_m - r_f) + alpha, zero noise
    return rf + BETA * (bench - rf) + DAILY_ALPHA


def test_jensens_alpha_recovers_true_alpha_with_rf_series():
    bench = _bench()
    rf = pd.Series(RF_DAILY, index=bench.index)
    asset = _asset(bench, rf)
    alpha = rolling_alpha(asset, bench, window=100, rf=rf, periods_per_year=252)
    assert alpha.dropna().iloc[-1] == pytest.approx(DAILY_ALPHA * 252, rel=1e-9)


def test_jensens_alpha_accepts_scalar_rf():
    bench = _bench()
    asset = _asset(bench, RF_DAILY)
    alpha = rolling_alpha(asset, bench, window=100, rf=RF_DAILY, periods_per_year=252)
    assert alpha.dropna().iloc[-1] == pytest.approx(DAILY_ALPHA * 252, rel=1e-9)


def test_raw_alpha_carries_the_documented_one_minus_beta_rf_bias():
    bench = _bench()
    asset = _asset(bench, RF_DAILY)
    raw = rolling_alpha(asset, bench, window=100, periods_per_year=252)  # rf omitted
    biased = (DAILY_ALPHA + (1 - BETA) * RF_DAILY) * 252
    assert raw.dropna().iloc[-1] == pytest.approx(biased, rel=1e-9)


def test_beta_unchanged_by_constant_rf():
    bench = _bench()
    asset = _asset(bench, RF_DAILY)
    b_raw = rolling_beta(asset, bench, window=100)
    b_ex = rolling_beta(asset, bench, window=100, rf=RF_DAILY)
    assert b_ex.dropna().iloc[-1] == pytest.approx(b_raw.dropna().iloc[-1], rel=1e-12)
    assert b_ex.dropna().iloc[-1] == pytest.approx(BETA, rel=1e-9)
