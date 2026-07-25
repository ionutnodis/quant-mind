import numpy as np
import pandas as pd
import pytest

from quantmind.risk.montecarlo import simulate_terminal_returns


def _returns_frame(n_days=1000, n_assets=2, scale=0.01, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n_days)
    data = rng.normal(0.0, scale, size=(n_days, n_assets))
    return pd.DataFrame(data, index=idx, columns=[f"A{i}" for i in range(n_assets)])


def test_seeded_run_is_reproducible():
    df = _returns_frame()
    w = np.array([0.5, 0.5])
    a = simulate_terminal_returns(df, w, n_paths=500, horizon=21, block_size=5, seed=42)
    b = simulate_terminal_returns(df, w, n_paths=500, horizon=21, block_size=5, seed=42)
    np.testing.assert_array_equal(a, b)


def test_different_seeds_differ():
    df = _returns_frame()
    w = np.array([0.5, 0.5])
    a = simulate_terminal_returns(df, w, n_paths=500, horizon=21, block_size=5, seed=1)
    b = simulate_terminal_returns(df, w, n_paths=500, horizon=21, block_size=5, seed=2)
    assert not np.array_equal(a, b)


def test_statistical_bounds_for_zero_mean_input():
    daily_vol = 0.01
    df = _returns_frame(n_days=4000, scale=daily_vol, seed=11)
    w = np.array([1.0, 0.0])
    horizon = 21
    terms = simulate_terminal_returns(df, w, n_paths=20_000, horizon=horizon, block_size=5, seed=3)
    assert terms.shape == (20_000,)
    # bootstrap resamples the empirical distribution: expected terminal mean tracks it
    assert abs(terms.mean() - horizon * df.iloc[:, 0].mean()) < 0.002
    expected_vol = daily_vol * np.sqrt(horizon)
    assert terms.std() == pytest.approx(expected_vol, rel=0.15)


def test_chunking_does_not_change_results():
    df = _returns_frame()
    w = np.array([0.3, 0.7])
    big = simulate_terminal_returns(df, w, n_paths=1000, horizon=10, block_size=5, seed=9, chunk_size=1000)
    small = simulate_terminal_returns(df, w, n_paths=1000, horizon=10, block_size=5, seed=9, chunk_size=64)
    np.testing.assert_array_equal(big, small)
