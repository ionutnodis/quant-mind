import numpy as np
import pandas as pd
import pytest

from quantmind.risk.factors import (
    FactorRegressionResult,
    bp_change_series,
    factor_regression,
    newey_west_lags,
    r_squared_progression,
)
from quantmind.risk.returns import InsufficientDataError


def _idx(n):
    return pd.bdate_range("2024-01-01", periods=n)


def _rand(n, seed, scale=0.01):
    rng = np.random.default_rng(seed)
    return pd.Series(np.random.default_rng(seed).normal(0.0, scale, n), index=_idx(n))


def test_newey_west_lags_matches_plugin_formula():
    # floor(4*(100/100)^(2/9)) = floor(4) = 4
    assert newey_west_lags(100) == 4
    # floor(4*(400/100)^(2/9)) = floor(4*4^(2/9)) ~= floor(5.14) = 5
    assert newey_west_lags(400) == 5
    assert newey_west_lags(0) == 0


def test_bp_change_series_hand_computed():
    levels = pd.Series([0.040, 0.0405, 0.039], index=_idx(3))
    bp = bp_change_series(levels)
    assert list(bp.round(4)) == [pytest.approx(5.0), pytest.approx(-15.0)]
    assert len(bp) == 2


def test_factor_regression_single_factor_recovers_capm_beta_alpha_no_noise():
    n = 300
    bench = _rand(n, seed=1)
    alpha_true, beta_true = 0.0003, 1.7
    y = alpha_true + beta_true * bench
    result = factor_regression(y, {"SPY": bench})
    assert result.alpha == pytest.approx(alpha_true, abs=1e-9)
    assert result.betas["SPY"] == pytest.approx(beta_true, abs=1e-9)
    assert result.r_squared == pytest.approx(1.0, abs=1e-9)
    assert result.n_obs == n
    assert result.variance_shares["SPY"] == pytest.approx(1.0, abs=1e-6)
    assert result.variance_shares["idiosyncratic"] == pytest.approx(0.0, abs=1e-6)


def test_factor_regression_two_factor_recovers_known_betas_and_decomposition():
    n = 400
    rng = np.random.default_rng(0)
    idx = _idx(n)
    f1 = pd.Series(rng.normal(0.0, 0.01, n), index=idx)
    f2 = pd.Series(rng.normal(0.0, 0.008, n), index=idx)
    noise = pd.Series(rng.normal(0.0, 0.002, n), index=idx)
    alpha_true, b1_true, b2_true = 0.0001, 1.3, -0.4
    y = alpha_true + b1_true * f1 + b2_true * f2 + noise

    result = factor_regression(y, {"F1": f1, "F2": f2})

    assert result.betas["F1"] == pytest.approx(b1_true, abs=0.05)
    assert result.betas["F2"] == pytest.approx(b2_true, abs=0.05)
    assert result.alpha == pytest.approx(alpha_true, abs=1e-3)

    # Exact-decomposition identity: shares sum to 1 (R^2 + idiosyncratic).
    total_share = sum(result.variance_shares.values())
    assert total_share == pytest.approx(1.0, abs=1e-8)
    assert result.variance_shares["idiosyncratic"] == pytest.approx(1.0 - result.r_squared, abs=1e-8)

    # Cross-check the per-factor share formula directly against hand-computed
    # covariances, independent of the implementation.
    y_aligned = pd.concat({"y": y, "F1": f1, "F2": f2}, axis=1).dropna()
    var_y = y_aligned["y"].var(ddof=1)
    expected_share_f1 = result.betas["F1"] * y_aligned["F1"].cov(y_aligned["y"]) / var_y
    assert result.variance_shares["F1"] == pytest.approx(expected_share_f1, abs=1e-9)

    # Exact attribution identity: contributions sum to mean(y).
    total_attribution = sum(result.attribution.values())
    assert total_attribution == pytest.approx(float(y_aligned["y"].mean()), abs=1e-9)
    assert result.attribution["idiosyncratic"] == pytest.approx(0.0, abs=1e-9)
    expected_attr_f1 = result.betas["F1"] * float(y_aligned["F1"].mean())
    assert result.attribution["F1"] == pytest.approx(expected_attr_f1, abs=1e-9)


def test_factor_regression_confidence_interval_is_well_formed_and_narrows_with_less_noise():
    n = 500
    rng = np.random.default_rng(3)
    bench = pd.Series(rng.normal(0.0, 0.01, n), index=_idx(n))
    beta_true = 0.9

    noisy = beta_true * bench + pd.Series(rng.normal(0.0, 0.02, n), index=_idx(n))
    quiet = beta_true * bench + pd.Series(rng.normal(0.0, 0.0005, n), index=_idx(n))

    noisy_result = factor_regression(noisy, {"SPY": bench}, confidence=0.95)
    quiet_result = factor_regression(quiet, {"SPY": bench}, confidence=0.95)

    for result in (noisy_result, quiet_result):
        lo, hi = result.beta_ci["SPY"]
        assert lo < result.betas["SPY"] < hi
        assert result.beta_se["SPY"] > 0
        alo, ahi = result.alpha_ci
        assert alo < result.alpha < ahi

    noisy_width = noisy_result.beta_ci["SPY"][1] - noisy_result.beta_ci["SPY"][0]
    quiet_width = quiet_result.beta_ci["SPY"][1] - quiet_result.beta_ci["SPY"][0]
    assert quiet_width < noisy_width


def test_factor_regression_requires_at_least_one_factor():
    y = _rand(100, seed=4)
    with pytest.raises(InsufficientDataError):
        factor_regression(y, {})


def test_factor_regression_insufficient_observations_is_explicit_error():
    y = _rand(20, seed=5)
    bench = _rand(20, seed=6)
    with pytest.raises(InsufficientDataError):
        factor_regression(y, {"SPY": bench})


def test_factor_regression_aligns_on_intersection_and_drops_nan():
    idx_a = pd.bdate_range("2024-01-01", periods=200)
    idx_b = pd.bdate_range("2024-01-10", periods=200)
    rng = np.random.default_rng(7)
    y = pd.Series(rng.normal(0.0, 0.01, 200), index=idx_a)
    f = pd.Series(rng.normal(0.0, 0.01, 200), index=idx_b)
    result = factor_regression(y, {"F": f})
    overlap = idx_a.intersection(idx_b)
    assert result.n_obs == len(overlap)


def test_r_squared_progression_is_monotonic_nondecreasing_and_matches_full_fit():
    n = 400
    rng = np.random.default_rng(8)
    idx = _idx(n)
    f1 = pd.Series(rng.normal(0.0, 0.01, n), index=idx)
    f2 = pd.Series(rng.normal(0.0, 0.008, n), index=idx)
    noise = pd.Series(rng.normal(0.0, 0.002, n), index=idx)
    y = 1.1 * f1 - 0.3 * f2 + noise

    steps = r_squared_progression(y, [("F1", f1), ("F2", f2)])
    assert [name for name, _ in steps] == ["F1", "F2"]
    r2_f1_only = steps[0][1]
    r2_both = steps[1][1]
    assert r2_both >= r2_f1_only - 1e-9

    single = factor_regression(y, {"F1": f1})
    assert r2_f1_only == pytest.approx(single.r_squared, abs=1e-9)
    full = factor_regression(y, {"F1": f1, "F2": f2})
    assert r2_both == pytest.approx(full.r_squared, abs=1e-9)


def test_r_squared_progression_requires_at_least_one_factor():
    y = _rand(100, seed=9)
    with pytest.raises(InsufficientDataError):
        r_squared_progression(y, [])


def test_factor_regression_result_is_frozen_dataclass():
    result = factor_regression(_rand(200, seed=10) * 0 + 1e-6 + _rand(200, seed=11), {"F": _rand(200, seed=11)})
    assert isinstance(result, FactorRegressionResult)
    with pytest.raises(Exception):
        result.alpha = 1.0  # type: ignore[misc]


def test_factor_regression_drops_non_finite_rows_instead_of_crashing():
    # A zero close upstream turns a pct_change observation into +/-inf, which
    # dropna() alone does NOT remove — statsmodels then raised
    # MissingDataError (not an InsufficientDataError, so routers 500'd —
    # batch-1 final review F2). Non-finite rows must be excluded from the
    # fit exactly like missing data.
    n = 300
    bench = _rand(n, seed=3)
    y = 0.0002 + 1.2 * bench
    y.iloc[10] = np.inf
    y.iloc[11] = -np.inf
    result = factor_regression(y, {"SPY": bench})
    assert result.n_obs == n - 2
    assert result.betas["SPY"] == pytest.approx(1.2, abs=1e-9)


def test_factor_regression_drops_inf_in_factor_series_too():
    n = 300
    bench = _rand(n, seed=4)
    y = 0.5 * bench
    factor = bench.copy()
    factor.iloc[5] = np.inf
    result = factor_regression(y, {"SPY": factor})
    assert result.n_obs == n - 1
    assert result.betas["SPY"] == pytest.approx(0.5, abs=1e-9)
