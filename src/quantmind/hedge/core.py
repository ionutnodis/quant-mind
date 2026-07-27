"""Hedge / construction core: drawdown, leverage headroom, diversification.

Pure functions (Engineering Constraint 2): pandas/numpy in, floats out;
picklable. Insufficient data is an explicit error, never a silent NaN.

These are *assumption-bound scenario* tools, not guarantees: leverage headroom
scales historical drawdown linearly and ignores margin liquidation, financing,
gap risk, options nonlinearity, and path-dependent rebalancing (design H4). It
answers "how much leverage would have kept my worst historical drawdown inside a
budget", not "how much leverage is safe."
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantmind.risk.returns import InsufficientDataError, _require


def max_drawdown(returns: pd.Series) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path, as a
    positive magnitude (0.20 == a 20% drawdown)."""
    _require(len(returns) >= 2, f"{len(returns)} observations cannot estimate a drawdown")
    cum = (1.0 + returns).cumprod()
    running_max = cum.cummax()
    drawdown = cum / running_max - 1.0
    return float(-drawdown.min())


def leverage_headroom(returns: pd.Series, drawdown_budget: float) -> float:
    """Scenario leverage L such that L x (historical max drawdown) equals the
    `drawdown_budget` (a target worst-case loss, e.g. 0.25 for 25%). L < 1 means
    the book already exceeds the budget and should de-lever; L > 1 means room to
    add. Assumption-bound (see module docstring) — NOT a safe-leverage
    guarantee. Undefined when the book never drew down."""
    if not drawdown_budget > 0.0:
        raise ValueError(f"drawdown_budget must be positive, got {drawdown_budget}")
    mdd = max_drawdown(returns)
    if not mdd > 0.0:
        raise ValueError("no historical drawdown; leverage headroom is undefined")
    return drawdown_budget / mdd


def diversification_ratio(returns: pd.DataFrame, weights: np.ndarray) -> float:
    """Choueifaty diversification ratio: (weighted-average asset vol) divided by
    the portfolio vol. 1.0 == no diversification (perfectly collinear book);
    higher == more structurally orthogonal legs. Scale-invariant, so raw daily
    vols are fine."""
    _require(returns.shape[1] >= 2, "diversification ratio needs >= 2 instruments")
    _require(len(returns) >= 2, f"{len(returns)} observations cannot estimate volatility")
    w = np.asarray(weights, dtype=float)
    vols = returns.std(ddof=1).to_numpy()
    weighted_avg_vol = float(np.abs(w) @ vols)
    port_returns = returns.to_numpy() @ w
    port_vol = float(pd.Series(port_returns).std(ddof=1))
    _require(port_vol > 0.0, "portfolio has zero variance; diversification ratio undefined")
    return weighted_avg_vol / port_vol
