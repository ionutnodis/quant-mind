"""Core cross-sectional analytics: returns, covariance, correlation.

Pure functions (Engineering Constraint 2): pandas in, pandas out, picklable.
Insufficient data is an explicit error, never a silent NaN.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantmind.analytics.correlation import correlation_matrix
from quantmind.risk.returns import InsufficientDataError, _require


def returns_matrix(
    prices: pd.DataFrame,
    method: str = "simple",
    align: str = "complete_case",
) -> pd.DataFrame:
    """Per-column period returns from a wide price frame, aligned to common dates.

    `prices` is wide: columns are opaque instrument labels (symbols or conIds),
    index is dates. method="simple" is per-column pct_change; method="log" is
    log(p / p.shift(1)). Both align="complete_case" and align="intersection" keep
    only rows where every column has a value (dropna across all columns) — the
    intersection of each column's non-NaN history.

    fill_method=None so gaps are never silently forward-filled into returns:
    non-overlapping histories drop out rather than fabricating a common window.
    """
    if method not in ("simple", "log"):
        raise ValueError(f"unknown method {method!r}; expected 'simple' or 'log'")
    if align not in ("complete_case", "intersection"):
        raise ValueError(f"unknown align {align!r}; expected 'complete_case' or 'intersection'")

    if method == "simple":
        rets = prices.pct_change(fill_method=None)
    else:
        rets = np.log(prices / prices.shift(1))

    rets = rets.dropna()
    _require(len(rets) >= 2, f"{len(rets)} common return rows; need >= 2 (non-overlapping histories?)")
    return rets


def covariance(returns: pd.DataFrame) -> pd.DataFrame:
    """Sample covariance matrix (ddof=1) of a returns frame."""
    _require(len(returns) >= 2, f"{len(returns)} observations cannot estimate covariance")
    return returns.cov()


def correlation(returns: pd.DataFrame) -> pd.DataFrame:
    """Sample correlation matrix of a returns frame.

    Delegates to quantmind.analytics.correlation.correlation_matrix (DRY).
    """
    _require(len(returns) >= 2, f"{len(returns)} observations cannot estimate correlation")
    return correlation_matrix(returns)
