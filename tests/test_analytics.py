import numpy as np
import pandas as pd
import pytest

from quantmind.analytics.correlation import correlation_matrix, rolling_correlation
from quantmind.analytics.cointegration import engle_granger


def _idx(n):
    return pd.bdate_range("2023-01-02", periods=n)


def test_correlation_matrix_symmetric_unit_diagonal():
    rng = np.random.default_rng(5)
    df = pd.DataFrame(rng.normal(size=(500, 3)), index=_idx(500), columns=list("abc"))
    m = correlation_matrix(df)
    assert np.allclose(np.diag(m), 1.0)
    assert np.allclose(m, m.T)


def test_perfectly_correlated_assets_show_corr_one():
    rng = np.random.default_rng(6)
    a = pd.Series(rng.normal(size=400), index=_idx(400))
    df = pd.DataFrame({"a": a, "b": 2.0 * a})
    m = correlation_matrix(df)
    assert m.loc["a", "b"] == pytest.approx(1.0)


def test_rolling_correlation_of_scaled_series_is_one():
    rng = np.random.default_rng(7)
    a = pd.Series(rng.normal(size=300), index=_idx(300))
    rc = rolling_correlation(a, 3.0 * a, window=60)
    assert rc.dropna().iloc[-1] == pytest.approx(1.0)


def test_engle_granger_detects_synthetic_cointegrated_pair():
    rng = np.random.default_rng(8)
    x = pd.Series(np.cumsum(rng.normal(size=1000)), index=_idx(1000))
    y = 0.5 * x + pd.Series(rng.normal(scale=0.5, size=1000), index=_idx(1000))
    res = engle_granger(y, x)
    assert res.pvalue < 0.05
    assert res.hedge_ratio == pytest.approx(0.5, abs=0.05)
    assert res.is_cointegrated()


def test_engle_granger_rejects_independent_random_walks():
    rng = np.random.default_rng(9)
    x = pd.Series(np.cumsum(rng.normal(size=1000)), index=_idx(1000))
    y = pd.Series(np.cumsum(rng.normal(size=1000)), index=_idx(1000))
    res = engle_granger(y, x)
    assert res.pvalue > 0.05
    assert not res.is_cointegrated()


def test_engle_granger_reports_hedge_ratio_se():
    # Uncertainty is displayed (wave-3 Global Constraint): the hedge ratio
    # ships a standard error. For the synthetic pair (true ratio 0.5, tight
    # stationary noise on a wide-ranging walk) the SE is positive and small
    # relative to the estimate.
    rng = np.random.default_rng(8)
    x = pd.Series(np.cumsum(rng.normal(size=1000)), index=_idx(1000))
    y = 0.5 * x + pd.Series(rng.normal(scale=0.5, size=1000), index=_idx(1000))
    res = engle_granger(y, x)
    assert res.hedge_ratio_se > 0
    assert res.hedge_ratio_se < 0.1 * abs(res.hedge_ratio)


def test_engle_granger_spread_passes_ou_mean_reversion_gate():
    # The Lab's pair pipeline (wave-3B): EG hedge ratio -> OU fit on the
    # spread. A genuinely cointegrated pair's spread must clear the
    # random-walk gate (delta AIC favors OU AND ADF rejects the unit root).
    from quantmind.models.ou import OrnsteinUhlenbeck

    rng = np.random.default_rng(8)
    x = pd.Series(np.cumsum(rng.normal(size=1000)), index=_idx(1000))
    y = 0.5 * x + pd.Series(rng.normal(scale=0.5, size=1000), index=_idx(1000))
    res = engle_granger(y, x)
    spread = y - res.hedge_ratio * x
    fit = OrnsteinUhlenbeck().fit(spread)
    assert fit.diagnostics["mean_reversion"] == 1.0
    assert fit.diagnostics["half_life_days"] > 0
