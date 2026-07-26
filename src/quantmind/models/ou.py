"""Ornstein-Uhlenbeck (mean-reverting) model — the Lab's first instrument.

Discretization: x[t+1] = x[t] + theta*(mu - x[t])*dt + sigma*sqrt(dt)*eps.
Fit via the exact AR(1) mapping: OLS of x[t+1] on x[t] gives intercept a and
slope b, with theta = (1-b)/dt, mu = a/(1-b), sigma = std(resid)/sqrt(dt).
CIs propagate the OLS standard errors (delta-method approximation for mu).
Diagnostics include ADF stationarity, AIC, and log-likelihood — the Lab shows
its math (DESIGN.md: mathematical transparency is a feature).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import chi2
from statsmodels.tsa.stattools import adfuller

from quantmind.models.base import Factor, FitResult


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
        return FitResult(
            model_name=self.name,
            params={"theta": float(theta), "mu": float(mu), "sigma": sigma},
            cis={
                "theta": (float(min(theta_ci)), float(max(theta_ci))),
                "mu": (float(mu_ci[0]), float(mu_ci[1])),
                "sigma": sigma_ci,
            },
            diagnostics={
                "adf_pvalue": adf_pvalue,
                "aic": float(ols.aic),
                "log_likelihood": float(ols.llf),
                "r_squared": float(ols.rsquared),
            },
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
        start = mu if x0 is None else x0
        paths = np.empty((n_paths, horizon))
        x = np.full(n_paths, start, dtype=float)
        shocks = rng.normal(size=(n_paths, horizon))
        for t in range(horizon):
            x = x + theta * (mu - x) * dt + sigma * math.sqrt(dt) * shocks[:, t]
            paths[:, t] = x
        return paths
