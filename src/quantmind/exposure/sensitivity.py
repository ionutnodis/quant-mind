"""Book sensitivity to standard macro shocks + regime-conditional rotation
stats (wave-3B "Macro book-aware").

Pure picklable functions (Engineering Constraint 2): pandas/numpy in,
dataclasses out; series resolution/pricing stays in the router.

Shock contract (mirrors quantmind.exposure.bridge's unit discipline): a
`Shock` is TYPED — its `kind` names the units its `size` is denominated in,
and `shock_factor` is the ONLY sanctioned way to turn a cached level/price
series into the regressor those units are valid against:

  * ``rate_bp``    — size in basis points; factor = daily level diff x 1e4
                     (quantmind.risk.factors.bp_change_series; FRED yields are
                     cached as decimals, and pct_change near zero is
                     degenerate — same rationale as routers/risk.py's
                     _RATE_LEVEL_SERIES).
  * ``return``     — size as a decimal return; factor = daily simple return.
  * ``vol_points`` — size in index points; factor = daily level diff (a VIX
                     16 -> 17 move is "+1 vol pt", not +6.25%).

An unknown kind raises `UnsupportedShockError` rather than mis-multiplying
(bridge.py's "never dimensionally wrong" rule).

`book_shock_sensitivity` regresses the book's daily return on the factor
(quantmind.risk.factors.factor_regression -> Newey-West HAC SEs, Global
Constraint: every regression estimate carries a CI/SE) and scales the beta
into dollars: dollar_response = beta x shock.size x book_gross. The SE/CI are
the SAME linear transform of the beta's SE/CI (exact, not approximated),
with the CI re-ordered when shock.size is negative.

`regime_conditional_returns` buckets dates by quantiles of a regime variable
(e.g. VIX close terciles) and reports each symbol's mean daily return + its
standard error per bucket — "in high-vol regimes, what led/lagged".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from quantmind.risk.factors import bp_change_series, factor_regression
from quantmind.risk.returns import InsufficientDataError, simple_returns


class UnsupportedShockError(ValueError):
    pass


# Standard shock sizes (the wave-3B spec: "define each shock size explicitly
# and label it") — labels render verbatim in the UI next to every estimate.
DEFAULT_RATE_SHOCK_BP = 10.0
DEFAULT_RETURN_SHOCK = 0.01
DEFAULT_VOL_SHOCK_POINTS = 5.0


@dataclass(frozen=True)
class Shock:
    driver: str
    kind: str  # "rate_bp" | "return" | "vol_points"
    size: float  # denominated in `kind`'s units (bp / decimal return / points)
    label: str  # human label, e.g. "+10bp" / "+1%" / "+5 vol pts"


def rate_shock(driver: str, size_bp: float = DEFAULT_RATE_SHOCK_BP) -> Shock:
    return Shock(driver=driver, kind="rate_bp", size=size_bp, label=f"{size_bp:+g}bp")


def return_shock(driver: str, size: float = DEFAULT_RETURN_SHOCK) -> Shock:
    return Shock(driver=driver, kind="return", size=size, label=f"{size * 100:+g}%")


def vol_shock(driver: str, size: float = DEFAULT_VOL_SHOCK_POINTS) -> Shock:
    return Shock(driver=driver, kind="vol_points", size=size, label=f"{size:+g} vol pts")


def shock_factor(series: pd.Series, kind: str) -> pd.Series:
    """The daily regressor whose units match a `Shock` of `kind` (see module
    docstring). `series` is the cached LEVEL/PRICE series of the driver."""
    if kind == "rate_bp":
        return bp_change_series(series)
    if kind == "return":
        return simple_returns(series)
    if kind == "vol_points":
        return series.diff().dropna()
    raise UnsupportedShockError(
        f"unknown shock kind {kind!r} — refusing to build a dimensionally wrong factor"
    )


@dataclass(frozen=True)
class SensitivityEstimate:
    driver: str
    shock_kind: str
    shock_size: float
    shock_label: str
    beta: float  # book daily return per factor unit
    beta_se: float
    dollar_response: float  # beta x shock_size x book_gross
    se: float  # |shock_size x book_gross| x beta_se
    ci: tuple[float, float]  # ordered low/high dollar bounds
    n_obs: int
    hac_lags: int


def book_shock_sensitivity(
    book_returns: pd.Series,
    factor: pd.Series,
    shock: Shock,
    book_gross: float,
    confidence: float = 0.95,
    hac_lags: int | None = None,
) -> SensitivityEstimate:
    """Estimated dollar response of a `book_gross`-dollar book to `shock`,
    from an OLS fit of the book's daily returns on `factor` (which MUST have
    been built by `shock_factor(series, shock.kind)` so units line up).
    HAC (Newey-West) SE/CI, linearly rescaled into dollars — exact, since the
    dollar response is a linear transform of the beta."""
    if not (book_gross > 0):
        raise ValueError(f"book_gross must be positive, got {book_gross!r}")
    result = factor_regression(
        book_returns, {shock.driver: factor}, confidence=confidence, hac_lags=hac_lags
    )
    beta = result.betas[shock.driver]
    beta_se = result.beta_se[shock.driver]
    scale = shock.size * book_gross
    ci_lo, ci_hi = result.beta_ci[shock.driver]
    # A numerically degenerate book/factor (e.g. constant returns whose
    # variance is only float noise) can slip past factor_regression's exact
    # zero-variance guard and come back with a NaN HAC SE/CI. An estimate
    # without a usable SE/CI must not be shown (Global Constraint: every
    # regression estimate carries a CI or SE) — refuse it honestly.
    if not all(math.isfinite(v) for v in (beta, beta_se, ci_lo, ci_hi)):
        raise InsufficientDataError(
            "regression SE/CI is non-finite — book or factor variance is degenerate"
        )
    lo, hi = sorted((ci_lo * scale, ci_hi * scale))
    return SensitivityEstimate(
        driver=shock.driver,
        shock_kind=shock.kind,
        shock_size=shock.size,
        shock_label=shock.label,
        beta=beta,
        beta_se=beta_se,
        dollar_response=beta * scale,
        se=beta_se * abs(scale),
        ci=(lo, hi),
        n_obs=result.n_obs,
        hac_lags=result.hac_lags,
    )


# --- regime-conditional rotation -------------------------------------------

_TERCILE_LABELS = ("low", "mid", "high")


@dataclass(frozen=True)
class RegimeBucketStats:
    bucket: str  # "low" / "mid" / "high" (terciles) or "q{i}" otherwise
    lo: float  # observed regime-variable min inside the bucket
    hi: float  # observed regime-variable max inside the bucket
    n_days: int
    mean_daily: dict[str, float]  # symbol -> mean daily return in the bucket
    se_daily: dict[str, float]  # symbol -> SE of that mean (NaN when n<2)


def regime_conditional_returns(
    returns: pd.DataFrame,
    regime_levels: pd.Series,
    n_buckets: int = 3,
) -> list[RegimeBucketStats]:
    """Per-symbol mean daily return + SE conditioned on quantile buckets of
    `regime_levels` (inner-joined on dates). Buckets come back lowest-regime
    first. Every mean carries its standard error (std/sqrt(n), NaN when the
    bucket has a single day) — Global Constraint: estimates carry CI/SE.
    Raises InsufficientDataError when there are fewer aligned days than
    buckets or the regime variable is too degenerate to split."""
    if returns.shape[1] == 0:
        raise InsufficientDataError("no return columns to condition")
    regime = regime_levels.reindex(returns.index).dropna()
    aligned = returns.loc[regime.index].dropna(how="any")
    regime = regime.loc[aligned.index]
    if len(aligned) < n_buckets:
        raise InsufficientDataError(
            f"{len(aligned)} aligned days insufficient for {n_buckets} regime buckets"
        )
    codes, bins = pd.qcut(regime, n_buckets, labels=False, retbins=True, duplicates="drop")
    if len(bins) - 1 < n_buckets:
        raise InsufficientDataError(
            f"regime variable has too few distinct values for {n_buckets} buckets"
        )
    labels = _TERCILE_LABELS if n_buckets == 3 else tuple(f"q{i + 1}" for i in range(n_buckets))
    buckets: list[RegimeBucketStats] = []
    for i in range(n_buckets):
        mask = codes == i
        sub = aligned.loc[mask.to_numpy()]
        rv = regime.loc[mask.to_numpy()]
        n = len(sub)
        mean_daily = {c: float(sub[c].mean()) for c in aligned.columns}
        se_daily = {
            c: (float(sub[c].std(ddof=1) / math.sqrt(n)) if n >= 2 else float("nan"))
            for c in aligned.columns
        }
        buckets.append(
            RegimeBucketStats(
                bucket=labels[i],
                lo=float(rv.min()),
                hi=float(rv.max()),
                n_days=n,
                mean_daily=mean_daily,
                se_daily=se_daily,
            )
        )
    return buckets
