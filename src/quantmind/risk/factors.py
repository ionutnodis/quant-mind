"""Factor regression: CAPM single-factor and multi-factor OLS decomposition.

Pure functions (Engineering Constraint 2): pandas/numpy in, dataclasses/floats
out. I/O (resolving symbols/named series from the store) stays in the router.

Every regression estimate ships a Newey-West (HAC) standard error / CI —
daily return series are autocorrelated enough that plain OLS's iid-error SEs
understate uncertainty (Global Constraints: "any regression estimate shown to
the user carries a CI or SE (HAC/Newey-West where autocorrelation matters)").

Variance decomposition and return attribution identities
----------------------------------------------------------
For y = alpha + sum_i(beta_i * f_i) + eps fit by OLS with an intercept:

  * Cov(fitted, eps) = 0 exactly (OLS orthogonality), so
    Var(y) = Var(fitted) + Var(eps) exactly -> R^2 = Var(fitted)/Var(y).
  * Cov(fitted, y) = Var(fitted) (substitute y = fitted + eps and use the
    orthogonality above), and Cov(fitted, y) = sum_i(beta_i * Cov(f_i, y))
    by linearity. So Var(fitted) = sum_i(beta_i * Cov(f_i, y)) EXACTLY: the
    per-factor share beta_i * Cov(f_i, y) / Var(y) is an exact additive
    decomposition of R^2 across factors (no leftover cross term), even when
    the factors are themselves correlated with each other.
  * mean(y) = alpha + sum_i(beta_i * mean(f_i)) + mean(eps) exactly, and
    mean(eps) = 0 exactly for OLS with an intercept — so
    {"alpha": alpha, factor_i: beta_i * mean(f_i), "idiosyncratic": mean(eps)}
    is an exact additive decomposition of the average return.

Both identities are used as-is below (not approximated), which is what makes
them golden-testable against a synthetic construction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm
from dataclasses import dataclass

from quantmind.risk.returns import InsufficientDataError

# Rule-of-thumb dof cushion: require at least this many observations per
# fitted parameter (intercept + one per factor), floored so a single-factor
# CAPM fit still demands a reasonable minimum sample.
_MIN_OBS_PER_PARAM = 10
_MIN_OBS_FLOOR = 30

# Trading days per year: annualizes the daily intercept/residual-vol into the
# information ratio (annualized appraisal ratio).
_PERIODS_PER_YEAR = 252


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InsufficientDataError(message)


def newey_west_lags(n_obs: int) -> int:
    """Newey & West (1994) automatic lag-selection plug-in:
    floor(4 * (n/100)^(2/9)). Deterministic, no cross-validation — the
    standard default for daily-return HAC standard errors."""
    if n_obs <= 0:
        return 0
    return int(np.floor(4.0 * (n_obs / 100.0) ** (2.0 / 9.0)))


def bp_change_series(levels: pd.Series) -> pd.Series:
    """Basis-point daily change of a decimal-level rate series (e.g. FRED
    yields cached as 0.045 = 4.5%): diff(levels) * 10000, first observation
    dropped. Yield levels sit near zero, so a percent-change transform is
    degenerate/explosive there; a level diff in basis points is the standard
    fixed-income factor construction (used for e.g. a US10Y factor)."""
    return (levels.diff() * 10000.0).dropna()


@dataclass(frozen=True)
class FactorRegressionResult:
    factor_names: tuple[str, ...]
    n_obs: int
    hac_lags: int
    alpha: float
    alpha_se: float
    alpha_ci: tuple[float, float]
    # HAC t-statistic of the intercept (alpha / alpha_se); NaN when alpha_se==0.
    alpha_tstat: float
    # Annualized appraisal ratio: alpha*sqrt(252) / residual_std_daily
    # (== annualized alpha / annualized residual vol). NaN when residual_std==0.
    information_ratio: float
    betas: dict[str, float]
    beta_se: dict[str, float]
    beta_ci: dict[str, tuple[float, float]]
    r_squared: float
    fitted: pd.Series
    residuals: pd.Series
    # Per-factor share of Var(y) explained + "idiosyncratic" (residual share).
    # Sums to 1.0 (see module docstring identity).
    variance_shares: dict[str, float]
    # Per-factor mean-return contribution + "alpha" + "idiosyncratic" (mean
    # residual, ~0). Sums to mean(y) (see module docstring identity).
    attribution: dict[str, float]


def factor_regression(
    y: pd.Series,
    factors: dict[str, pd.Series],
    confidence: float = 0.95,
    hac_lags: int | None = None,
    rf: pd.Series | float | None = None,
    market_factor: str | None = None,
) -> FactorRegressionResult:
    """OLS regression of `y` on one or more `factors` (name -> series, inner-
    joined/aligned on index, NaN rows dropped), with Newey-West HAC standard
    errors. `factors` must be non-empty — pass a single benchmark series for
    a plain CAPM fit.

    `hac_lags` overrides the automatic Newey-West plug-in (`newey_west_lags`)
    when set; primarily a test seam.

    Excess-return (Jensen) alpha: pass a daily risk-free `rf` (scalar or series)
    to subtract it from `y` (excess asset return) and — when `market_factor`
    names one of the factors — from that ONE factor column (excess market
    return), leaving every other factor unchanged. The resulting intercept is
    then a true excess-return Jensen alpha rather than a raw-return drift. `rf`
    is aligned on the shared index (concat/dropna) before subtracting. With
    `rf=None` (the default) behavior is unchanged (raw-return intercept).
    """
    _require(len(factors) > 0, "factor_regression requires at least one factor")
    names = list(factors.keys())
    if market_factor is not None:
        _require(
            market_factor in names,
            f"market_factor {market_factor!r} is not among the factors {names}",
        )
    rf_is_series = isinstance(rf, pd.Series)
    frames = {"y": y, **factors}
    if rf_is_series:
        # Carry rf through the same concat/dropna so alignment is consistent:
        # rows missing rf are dropped alongside rows missing y/factors.
        frames["__rf__"] = rf
    aligned = pd.concat(frames, axis=1).dropna()

    if rf is not None:
        rf_col = aligned.pop("__rf__") if rf_is_series else float(rf)
        aligned["y"] = aligned["y"] - rf_col
        if market_factor is not None:
            aligned[market_factor] = aligned[market_factor] - rf_col

    n_obs = len(aligned)
    k = len(names)
    min_obs = max(_MIN_OBS_FLOOR, _MIN_OBS_PER_PARAM * (k + 1))
    _require(
        n_obs >= min_obs,
        f"{n_obs} overlapping observations insufficient for a {k}-factor regression (need >= {min_obs})",
    )

    y_aligned = aligned["y"]
    x = sm.add_constant(aligned[names])
    lags = hac_lags if hac_lags is not None else newey_west_lags(n_obs)
    model = sm.OLS(y_aligned, x).fit(cov_type="HAC", cov_kwds={"maxlags": lags, "use_correction": True})

    ci = model.conf_int(alpha=1.0 - confidence)
    alpha = float(model.params["const"])
    alpha_se = float(model.bse["const"])
    alpha_ci = (float(ci.loc["const", 0]), float(ci.loc["const", 1]))

    betas = {name: float(model.params[name]) for name in names}
    beta_se = {name: float(model.bse[name]) for name in names}
    beta_ci = {name: (float(ci.loc[name, 0]), float(ci.loc[name, 1])) for name in names}

    fitted = model.fittedvalues
    residuals = model.resid
    r_squared = float(model.rsquared)

    # HAC t-stat of the intercept, and the annualized appraisal ratio.
    # Annualized alpha = alpha*252 and annualized residual vol = resid_std*sqrt(252),
    # so their ratio collapses to alpha*sqrt(252)/resid_std_daily.
    alpha_tstat = alpha / alpha_se if alpha_se > 0 else float("nan")
    resid_std_daily = float(residuals.std(ddof=1))
    information_ratio = (
        alpha * np.sqrt(_PERIODS_PER_YEAR) / resid_std_daily
        if resid_std_daily > 0
        else float("nan")
    )

    var_y = float(y_aligned.var(ddof=1))
    _require(var_y > 0, "y has zero variance; cannot decompose")
    resid_var = float(residuals.var(ddof=1))

    variance_shares: dict[str, float] = {}
    for name in names:
        cov_fy = float(aligned[name].cov(y_aligned))
        variance_shares[name] = betas[name] * cov_fy / var_y
    variance_shares["idiosyncratic"] = resid_var / var_y

    attribution: dict[str, float] = {"alpha": alpha}
    for name in names:
        attribution[name] = betas[name] * float(aligned[name].mean())
    attribution["idiosyncratic"] = float(residuals.mean())

    return FactorRegressionResult(
        factor_names=tuple(names),
        n_obs=n_obs,
        hac_lags=lags,
        alpha=alpha,
        alpha_se=alpha_se,
        alpha_ci=alpha_ci,
        alpha_tstat=alpha_tstat,
        information_ratio=information_ratio,
        betas=betas,
        beta_se=beta_se,
        beta_ci=beta_ci,
        r_squared=r_squared,
        fitted=fitted,
        residuals=residuals,
        variance_shares=variance_shares,
        attribution=attribution,
    )


def r_squared_progression(
    y: pd.Series,
    factors_ordered: list[tuple[str, pd.Series]],
    confidence: float = 0.95,
    hac_lags: int | None = None,
) -> list[tuple[str, float]]:
    """Cumulative R^2 as factors are added one at a time, in the given order:
    step i includes `factors_ordered[0..i]`. Answers "how much incremental
    explanatory power does each additional factor add" — the single-factor
    CAPM fit is always step 0. Reuses `factor_regression` per step so the
    same dof/alignment rules apply throughout."""
    _require(len(factors_ordered) > 0, "r_squared_progression requires at least one factor")
    steps: list[tuple[str, float]] = []
    included: dict[str, pd.Series] = {}
    for name, series in factors_ordered:
        included[name] = series
        result = factor_regression(y, included, confidence=confidence, hac_lags=hac_lags)
        steps.append((name, result.r_squared))
    return steps
