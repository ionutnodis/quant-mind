"""Volatility-regime tagging from a VIX level series.

Pure function (Engineering Constraint 2): pandas in, pandas out, picklable.
Insufficient data is an explicit error, never a silent NaN.

The tagger buckets each observation into "low" / "med" / "high" volatility by
VIX terciles (data-driven quantiles, not fixed thresholds — a fixed "VIX > 20 =
high" ages badly as the vol regime shifts). It answers "what volatility regime
was the market in on this date", which downstream views condition on (e.g.
regime-split correlations or drag).
"""

from __future__ import annotations

import pandas as pd

from quantmind.risk.returns import InsufficientDataError, _require


def regime_tag(vix: pd.Series, low_q: float = 1.0 / 3.0, high_q: float = 2.0 / 3.0) -> pd.Series:
    """Label each VIX observation "low"/"med"/"high" by its tercile.

    `low_q`/`high_q` are the quantile cut points (defaults: the 1/3 and 2/3
    quantiles). A value at or below the `low_q` quantile is "low", at or below
    the `high_q` quantile is "med", otherwise "high". NaNs are dropped (never
    silently mislabeled); at least three finite observations are required to
    form terciles.
    """
    if not (0.0 < low_q < high_q < 1.0):
        raise ValueError(f"require 0 < low_q < high_q < 1, got low_q={low_q}, high_q={high_q}")
    clean = vix.dropna().astype(float)
    _require(len(clean) >= 3, f"{len(clean)} finite VIX observations cannot form terciles (need >= 3)")

    lo = clean.quantile(low_q)
    hi = clean.quantile(high_q)

    def _label(v: float) -> str:
        if v <= lo:
            return "low"
        if v <= hi:
            return "med"
        return "high"

    return clean.map(_label)
