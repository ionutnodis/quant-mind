"""Data-quality gate (Engineering Constraint 7).

Sits between datastore/ and risk/: no series reaches the risk engine without a
quality report, and cross-market series align on an explicit union calendar
with a flagged fill rule — never silent forward-fill.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class QualityReport:
    nan_run_max: int
    n_missing_days: int
    n_zero_volume: int

    @property
    def ok(self) -> bool:
        return self.nan_run_max == 0 and self.n_missing_days == 0 and self.n_zero_volume == 0


def quality_report(prices: pd.Series, volume: pd.Series | None = None) -> QualityReport:
    isna = prices.isna().to_numpy()
    nan_run_max = 0
    run = 0
    for missing in isna:
        run = run + 1 if missing else 0
        nan_run_max = max(nan_run_max, run)

    expected = pd.bdate_range(prices.index.min(), prices.index.max())
    n_missing = len(expected.difference(prices.index))

    n_zero_volume = int((volume == 0).sum()) if volume is not None else 0
    return QualityReport(nan_run_max=nan_run_max, n_missing_days=n_missing, n_zero_volume=n_zero_volume)


def align_calendars(series: dict[str, pd.Series]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align series from different market calendars onto their union index.

    Returns (aligned, filled): gaps are forward-filled but every filled cell is
    True in the `filled` mask. Leading gaps (before a series' first observation)
    stay NaN — history is never fabricated.
    """
    union = pd.DatetimeIndex(sorted(set().union(*(s.index for s in series.values()))))
    raw = pd.DataFrame({name: s.reindex(union) for name, s in series.items()})
    aligned = raw.ffill()
    filled = raw.isna() & aligned.notna()
    return aligned, filled
