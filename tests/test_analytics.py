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
