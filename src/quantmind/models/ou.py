"""Ornstein-Uhlenbeck (mean-reverting) model — the Lab's first instrument.

Discretization: x[t+1] = x[t] + theta*(mu - x[t])*dt + sigma*sqrt(dt)*eps.
Fit via the exact AR(1) mapping: OLS of x[t+1] on x[t] gives intercept a and
slope b, with theta = (1-b)/dt, mu = a/(1-b), sigma = std(resid)/sqrt(dt).
CIs propagate the OLS standard errors (delta-method approximation for mu).
Diagnostics include ADF stationarity, AIC, and log-likelihood — the Lab shows
its math (DESIGN.md: mathematical transparency is a feature).

Wave-3B practitioner readouts ride along on every fit (models/diagnostics.py):
half-life ln2/θ with delta-method CI, current displacement from μ in
stationary-σ units, the last observation (`x_last` — simulate's default x0),
and the random-walk gate (ΔAIC + LR vs the unit-root null, composed with ADF
into a single `mean_reversion` verdict). Derived keys are only added when
finite/defined — the API's FitResponse.diagnostics is dict[str, float], and
an undefined half-life is an ABSENT key, never a NaN. The half-life CI lives
in diagnostics (half_life_ci_lo/hi) rather than `cis` because `cis` keys are
contractually the parameter names (tests/test_api.py pins that set).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import chi2
from statsmodels.tsa.stattools import adfuller

from quantmind.models.base import Factor, FitResult
from quantmind.models.diagnostics import (
    displacement_sigma,
    half_life_days,
    rw_gate,
    stationary_sigma,
)


class OrnsteinUhlenbeck:
    name = "ou"
    factor = Factor(kind="rate_level", units="decimal", dt=1 / 252)

    def param_schema(self) -> dict:
        return {
            "name": self.name,
            "label": "Ornstein-Uhlenbeck (mean-reverting)",
            "factor": {"kind": self.factor.kind, "units": self.factor.units, "dt": self.factor.dt},
            "params": {
                "theta": {"label": "θ (mean-reversion speed, /yr)", "type": "float"},
                "mu": {"label": "μ (long-run level)", "type": "float"},
                "sigma": {"label": "σ (volatility, /√yr)", "type": "float"},
            },
        }

    def fit(self, series: pd.Series) -> FitResult:
        x = series.dropna().to_numpy()
        dt = self.factor.dt
        x_lag, x_next = x[:-1], x[1:]
        ols = sm.OLS(x_next, sm.add_constant(x_lag)).fit()
        a, b = ols.params
        theta = (1.0 - b) / dt
        mu = a / (1.0 - b)
        resid = ols.resid
        sigma = float(np.std(resid, ddof=2) / math.sqrt(dt))

        # CIs: theta from slope SE; mu via delta method on (a, b); sigma via chi-square
        se_a, se_b = ols.bse
        theta_ci = ((1.0 - (b + 1.96 * se_b)) / dt, (1.0 - (b - 1.96 * se_b)) / dt)
        cov = ols.cov_params()
        # mu = a/(1-b): grad = [1/(1-b), a/(1-b)^2]
        g = np.array([1.0 / (1.0 - b), a / (1.0 - b) ** 2])
        se_mu = float(np.sqrt(g @ cov @ g))
        mu_ci = (mu - 1.96 * se_mu, mu + 1.96 * se_mu)
        n = len(resid)
        var = np.var(resid, ddof=2)
        sigma_ci = (
            float(np.sqrt((n - 2) * var / chi2.ppf(0.975, n - 2)) / math.sqrt(dt)),
            float(np.sqrt((n - 2) * var / chi2.ppf(0.025, n - 2)) / math.sqrt(dt)),
        )

        adf_pvalue = float(adfuller(x, autolag="AIC")[1])
        gate = rw_gate(x)
        diagnostics: dict[str, float] = {
            "adf_pvalue": adf_pvalue,
            # ONE AIC convention across the exported trio (fix round 1): the
            # grid renders AIC, AIC (RW) and ΔAIC (RW−OU) side by side, so
            # they must close exactly (aic_rw − aic == delta_aic). The gate's
            # aic_ou (k counts σ, models/diagnostics.py) deliberately replaces
            # statsmodels' ols.aic here — ols.aic omits σ from k and would
            # make the rendered difference contradict ΔAIC by exactly 2.
            "aic": gate.aic_ou,
            "log_likelihood": float(ols.llf),
            "r_squared": float(ols.rsquared),
        }

        # Practitioner readouts (see module docstring): only finite/defined
        # values are added — FitResponse.diagnostics is dict[str, float].
        def put(key: str, value: float | None) -> None:
            if value is not None and math.isfinite(value):
                diagnostics[key] = float(value)

        x_last = float(x[-1])
        put("x_last", x_last)

        put("aic_rw", gate.aic_rw)
        put("delta_aic", gate.delta_aic)
        put("lr_stat", gate.lr_stat)
        # Mean reversion is ESTABLISHED only when θ > 0, the OU alternative
        # beats the RW null on AIC, AND ADF rejects the unit root at 5% —
        # ΔAIC/LR alone have no valid unit-root distribution (compose with
        # ADF, don't duplicate it).
        established = theta > 0 and gate.delta_aic > 0 and adf_pvalue < 0.05
        diagnostics["mean_reversion"] = 1.0 if established else 0.0

        theta_se = float(se_b) / dt
        hl = half_life_days(theta, theta_se, dt)
        if hl is not None:
            hl_value, (hl_lo, hl_hi) = hl
            put("half_life_days", hl_value)
            put("half_life_ci_lo", hl_lo)
            put("half_life_ci_hi", hl_hi)
        put("stationary_sigma", stationary_sigma(theta, sigma))
        put("displacement_sigma", displacement_sigma(x_last, float(mu), theta, sigma))

        return FitResult(
            model_name=self.name,
            params={"theta": float(theta), "mu": float(mu), "sigma": sigma},
            cis={
                "theta": (float(min(theta_ci)), float(max(theta_ci))),
                "mu": (float(mu_ci[0]), float(mu_ci[1])),
                "sigma": sigma_ci,
            },
            diagnostics=diagnostics,
            n_obs=n,
        )

    def simulate(
        self,
        fit: FitResult,
        horizon: int,
        n_paths: int,
        seed: int | None = None,
        x0: float | None = None,
    ) -> np.ndarray:
        theta, mu, sigma = fit.params["theta"], fit.params["mu"], fit.params["sigma"]
        dt = self.factor.dt
        rng = np.random.default_rng(seed)
        # Default start = the fitted series' last observation ("simulate from
        # where reality is", wave-3B) — falling back to mu only for fits that
        # predate the x_last diagnostic. Always overridable via x0.
        start = x0 if x0 is not None else fit.diagnostics.get("x_last", mu)
        paths = np.empty((n_paths, horizon))
        x = np.full(n_paths, start, dtype=float)
        shocks = rng.normal(size=(n_paths, horizon))
        for t in range(horizon):
            x = x + theta * (mu - x) * dt + sigma * math.sqrt(dt) * shocks[:, t]
            paths[:, t] = x
        return paths
