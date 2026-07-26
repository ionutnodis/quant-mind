"""Model registry + OU: the Lab's first instrument. Tests written before implementation."""

import math

import numpy as np
import pandas as pd
import pytest

from quantmind.models.base import Factor
from quantmind.models.diagnostics import (
    displacement_sigma,
    half_life_days,
    rw_gate,
    stationary_sigma,
)
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


# --- derived diagnostics: hand-computed goldens (wave-3B Lab practitioner) ---


def test_half_life_hand_computed():
    # theta = 2 /yr, dt = 1/252: HL = ln2/theta years = ln2/(theta*dt) days.
    result = half_life_days(theta=2.0, theta_se=0.5, dt=DT)
    assert result is not None
    hl, (lo, hi) = result
    assert hl == pytest.approx(math.log(2.0) / 2.0 * 252)  # 87.3365... days
    # Delta method: d(HL)/dθ = -ln2/θ² → se_days = ln2·se_θ/(θ²·dt)
    se = math.log(2.0) / 4.0 * 0.5 * 252  # 21.834... days
    assert lo == pytest.approx(hl - 1.96 * se)
    assert hi == pytest.approx(hl + 1.96 * se)


def test_half_life_undefined_without_mean_reversion():
    assert half_life_days(theta=0.0, theta_se=0.1, dt=DT) is None
    assert half_life_days(theta=-1.0, theta_se=0.1, dt=DT) is None


def test_half_life_ci_lower_bound_clamped_at_zero():
    # Weak reversion + huge SE: the delta-method interval would go negative —
    # a half-life can't, so the lower bound floors at 0 (honest, not absurd).
    result = half_life_days(theta=0.5, theta_se=2.0, dt=DT)
    assert result is not None
    _, (lo, _) = result
    assert lo == 0.0


def test_displacement_hand_computed():
    # Stationary sd = σ/√(2θ) = 0.01/2 = 0.005; z = (0.06-0.04)/0.005 = 4σ.
    assert stationary_sigma(theta=2.0, sigma=0.01) == pytest.approx(0.005)
    assert displacement_sigma(x_last=0.06, mu=0.04, theta=2.0, sigma=0.01) == pytest.approx(4.0)


def test_displacement_undefined_without_mean_reversion():
    assert stationary_sigma(theta=0.0, sigma=0.01) is None
    assert displacement_sigma(x_last=0.06, mu=0.04, theta=-1.0, sigma=0.01) is None


def test_rw_gate_prefers_ou_on_mean_reverting_series():
    gate = rw_gate(_synthetic_ou().to_numpy())
    assert gate.delta_aic > 0  # OU beats the RW null on AIC
    assert gate.lr_stat > 3.84  # LR strongly rejects b=1


def test_rw_gate_prefers_rw_on_random_walk():
    rng = np.random.default_rng(21)
    gate = rw_gate(np.cumsum(rng.normal(size=2000)))
    assert gate.delta_aic <= 0  # RW wins: mean reversion not established


def test_rw_gate_aic_lr_identity():
    # AIC = 2k - 2llf with k_ou=3 (a,b,σ), k_rw=2 (drift,σ):
    # ΔAIC = AIC_rw - AIC_ou = 2(llf_ou - llf_rw) - 2 = LR - 2 exactly.
    gate = rw_gate(_synthetic_ou(n=500).to_numpy())
    assert gate.delta_aic == pytest.approx(gate.lr_stat - 2.0)


def test_rw_gate_rejects_degenerate_input():
    with pytest.raises(ValueError):
        rw_gate(np.array([1.0, 2.0]))
    with pytest.raises(ValueError):
        rw_gate(np.full(100, 3.0))  # constant series: zero-variance residuals


# --- fit-level integration: every OU fit carries the practitioner readouts ---


def test_fit_reports_half_life_and_displacement_diagnostics():
    series = _synthetic_ou()
    fit = OrnsteinUhlenbeck().fit(series)
    d = fit.diagnostics
    theta, mu, sigma = fit.params["theta"], fit.params["mu"], fit.params["sigma"]
    assert d["half_life_days"] == pytest.approx(math.log(2.0) / theta * 252)
    assert d["half_life_ci_lo"] <= d["half_life_days"] <= d["half_life_ci_hi"]
    assert d["half_life_ci_hi"] > d["half_life_ci_lo"]
    stat_sd = sigma / math.sqrt(2.0 * theta)
    assert d["stationary_sigma"] == pytest.approx(stat_sd)
    assert d["displacement_sigma"] == pytest.approx((float(series.iloc[-1]) - mu) / stat_sd)
    assert d["x_last"] == pytest.approx(float(series.iloc[-1]))


def test_fit_random_walk_gate_composes_aic_and_adf():
    fit = OrnsteinUhlenbeck().fit(_synthetic_ou())
    assert fit.diagnostics["mean_reversion"] == 1.0
    assert fit.diagnostics["delta_aic"] > 0
    assert fit.diagnostics["lr_stat"] > 0

    rng = np.random.default_rng(33)
    rw = pd.Series(
        5.0 + np.cumsum(rng.normal(size=2000)) / 100.0,
        index=pd.bdate_range("2010-01-01", periods=2000),
    )
    fit_rw = OrnsteinUhlenbeck().fit(rw)
    assert fit_rw.diagnostics["mean_reversion"] == 0.0
    assert "delta_aic" in fit_rw.diagnostics
    assert "lr_stat" in fit_rw.diagnostics


def test_simulate_defaults_x0_to_last_observation():
    model = OrnsteinUhlenbeck()
    series = _synthetic_ou()
    fit = model.fit(series)
    x_last = float(series.iloc[-1])
    default = model.simulate(fit, horizon=5, n_paths=64, seed=9)
    explicit = model.simulate(fit, horizon=5, n_paths=64, seed=9, x0=x_last)
    np.testing.assert_array_equal(default, explicit)  # default start = last obs
    shifted = model.simulate(fit, horizon=5, n_paths=64, seed=9, x0=x_last + 0.05)
    assert not np.array_equal(default, shifted)  # still overridable


def test_registry_serves_schema_and_rejects_unknown():
    schemas = list_model_schemas()
    ou = next(s for s in schemas if s["name"] == "ou")
    assert ou["factor"]["kind"] == "rate_level"
    assert "theta" in ou["params"]
    assert get_model("ou") is not None
    with pytest.raises(KeyError, match="nope"):
        get_model("nope")
