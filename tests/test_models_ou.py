"""Model registry + OU: the Lab's first instrument. Tests written before implementation."""

import numpy as np
import pandas as pd
import pytest

from quantmind.models.base import Factor
from quantmind.models.registry import get_model, list_model_schemas
from quantmind.models.ou import OrnsteinUhlenbeck

DT = 1 / 252
THETA, MU, SIGMA = 2.0, 0.04, 0.01


def _synthetic_ou(n=5000, seed=42, x0=0.03):
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = x0
    for t in range(1, n):
        x[t] = x[t - 1] + THETA * (MU - x[t - 1]) * DT + SIGMA * np.sqrt(DT) * rng.normal()
    return pd.Series(x, index=pd.bdate_range("2010-01-01", periods=n))


def test_fit_recovers_known_parameters():
    fit = OrnsteinUhlenbeck().fit(_synthetic_ou())
    assert fit.params["theta"] == pytest.approx(THETA, rel=0.5)  # theta is hard; wide but bounded
    assert fit.params["mu"] == pytest.approx(MU, abs=0.005)
    assert fit.params["sigma"] == pytest.approx(SIGMA, rel=0.05)


def test_fit_reports_confidence_intervals_containing_estimates():
    fit = OrnsteinUhlenbeck().fit(_synthetic_ou())
    for name in ("theta", "mu", "sigma"):
        lo, hi = fit.cis[name]
        assert lo <= fit.params[name] <= hi
        assert hi > lo


def test_fit_diagnostics_include_stationarity_and_information_criteria():
    fit = OrnsteinUhlenbeck().fit(_synthetic_ou())
    assert fit.diagnostics["adf_pvalue"] < 0.10  # strongly mean-reverting series rejects unit root
    assert "aic" in fit.diagnostics
    assert "log_likelihood" in fit.diagnostics


def test_simulate_is_seeded_reproducible_and_shaped():
    model = OrnsteinUhlenbeck()
    fit = model.fit(_synthetic_ou())
    a = model.simulate(fit, horizon=126, n_paths=500, seed=7, x0=0.04)
    b = model.simulate(fit, horizon=126, n_paths=500, seed=7, x0=0.04)
    np.testing.assert_array_equal(a, b)
    assert a.shape == (500, 126)


def test_simulated_long_run_mean_reverts_to_mu():
    model = OrnsteinUhlenbeck()
    fit = model.fit(_synthetic_ou())
    paths = model.simulate(fit, horizon=2520, n_paths=2000, seed=11, x0=0.08)  # start far from mu
    terminal = paths[:, -1]
    assert terminal.mean() == pytest.approx(fit.params["mu"], abs=0.005)


def test_model_declares_typed_factor():
    f = OrnsteinUhlenbeck().factor
    assert isinstance(f, Factor)
    assert f.kind == "rate_level"
    assert f.units == "decimal"
    assert f.dt == pytest.approx(DT)


def test_registry_serves_schema_and_rejects_unknown():
    schemas = list_model_schemas()
    ou = next(s for s in schemas if s["name"] == "ou")
    assert ou["factor"]["kind"] == "rate_level"
    assert "theta" in ou["params"]
    assert get_model("ou") is not None
    with pytest.raises(KeyError, match="nope"):
        get_model("nope")
