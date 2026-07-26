"""Returns-based risk metrics: rolling beta/alpha, historical Expected Shortfall.

Pure functions (Engineering Constraint 2): pandas in, pandas/floats out.
Insufficient data is an explicit error, never a silent NaN.
"""

from __future__ import annotations

import math

import pandas as pd


class InsufficientDataError(ValueError):
    pass


def simple_returns(prices: pd.Series) -> pd.Series:
    return prices.pct_change().dropna()


def _excess(returns: pd.Series, rf: float | pd.Series | None) -> pd.Series:
    if rf is None:
        return returns
    return returns - rf


def rolling_beta(
    asset: pd.Series,
    benchmark: pd.Series,
    window: int,
    rf: float | pd.Series | None = None,
) -> pd.Series:
    """Rolling CAPM beta: excess asset returns regressed on excess benchmark returns.

    `rf` is a DAILY risk-free rate (scalar or aligned series, e.g. FRED 3M T-bill / 252).
    With rf=None this degrades to the raw-return market model — numerically near-identical
    for beta, since a near-constant rf barely moves cov/var.
    """
    _require(len(asset) >= window and len(benchmark) >= window, f"window {window} exceeds data length")
    a, b = _excess(asset, rf), _excess(benchmark, rf)
    cov = a.rolling(window).cov(b)
    var = b.rolling(window).var()
    return cov / var


def rolling_alpha(
    asset: pd.Series,
    benchmark: pd.Series,
    window: int,
    rf: float | pd.Series | None = None,
    periods_per_year: int = 252,
) -> pd.Series:
    """Annualized rolling Jensen's alpha: intercept of the CAPM excess-return regression,
    (r_a - r_f) = alpha + beta (r_m - r_f).

    With rf=None this is the raw-return market model, which overstates alpha by
    (1 - beta) * rf annually — pass rf for the honest number. The dashboard labels
    this "alpha vs <benchmark>": single-factor, so factor tilts still land in alpha.
    """
    a, b = _excess(asset, rf), _excess(benchmark, rf)
    beta = rolling_beta(asset, benchmark, window, rf=rf)
    daily_alpha = a.rolling(window).mean() - beta * b.rolling(window).mean()
    return daily_alpha * periods_per_year


def annualized_vol(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized volatility: sample std of period returns scaled by sqrt(periods_per_year)."""
    _require(len(returns) >= 2, f"{len(returns)} observations cannot estimate volatility")
    return float(returns.std() * math.sqrt(periods_per_year))


def historical_es(returns: pd.Series, confidence: float = 0.975) -> float:
    """Historical Expected Shortfall: mean of the worst floor(n*(1-confidence))
    observations, reported as a positive loss magnitude."""
    # epsilon guards float representation: 20*(1-0.9) is 1.9999... and must floor to 2
    n_tail = math.floor(len(returns) * (1.0 - confidence) + 1e-9)
    _require(n_tail >= 1, f"{len(returns)} observations give an empty tail at confidence {confidence}")
    tail = returns.sort_values().iloc[:n_tail]
    return float(-tail.mean())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InsufficientDataError(message)
