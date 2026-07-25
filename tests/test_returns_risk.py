import numpy as np
import pandas as pd
import pytest

from quantmind.risk.returns import (
    InsufficientDataError,
    historical_es,
    rolling_alpha,
    rolling_beta,
    simple_returns,
)


def _rand_returns(n, seed, scale=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(rng.normal(0.0, scale, n), index=idx)


def test_simple_returns_golden_values():
    prices = pd.Series([100.0, 110.0, 99.0], index=pd.bdate_range("2026-01-05", periods=3))
    r = simple_returns(prices)
    assert r.iloc[0] == pytest.approx(0.10)
    assert r.iloc[1] == pytest.approx(-0.10)
    assert len(r) == 2


def test_rolling_beta_recovers_known_beta_exactly_without_noise():
    bench = _rand_returns(300, seed=1)
    asset = 2.0 * bench  # beta exactly 2, zero idiosyncratic noise
    beta = rolling_beta(asset, bench, window=60)
    assert beta.dropna().iloc[-1] == pytest.approx(2.0)
    assert beta.dropna().min() == pytest.approx(2.0)


def test_rolling_alpha_recovers_known_drift():
    bench = _rand_returns(300, seed=2)
    daily_alpha = 0.001
    asset = 1.5 * bench + daily_alpha
    alpha = rolling_alpha(asset, bench, window=100, periods_per_year=252)
    assert alpha.dropna().iloc[-1] == pytest.approx(daily_alpha * 252, rel=1e-6)


def test_historical_es_hand_computed_case():
    # 20 returns; at 90% confidence the tail is the worst 2 observations.
    values = [-0.10, -0.05] + [0.01] * 18
    r = pd.Series(values, index=pd.bdate_range("2026-01-05", periods=20))
    es = historical_es(r, confidence=0.90)
    # ES = mean of the worst 10% = mean(-0.10, -0.05) = -0.075, reported as positive loss
    assert es == pytest.approx(0.075)


def test_historical_es_requires_at_least_one_tail_observation():
    r = pd.Series([0.01] * 10, index=pd.bdate_range("2026-01-05", periods=10))
    with pytest.raises(InsufficientDataError):
        historical_es(r, confidence=0.999)


def test_rolling_beta_window_larger_than_data_is_explicit_error():
    bench = _rand_returns(30, seed=3)
    with pytest.raises(InsufficientDataError):
        rolling_beta(bench, bench, window=60)
