"""Correlation toolkit: matrix and rolling pairwise correlations. Pure functions."""

from __future__ import annotations

import pandas as pd


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.corr()


def rolling_correlation(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    return a.rolling(window).corr(b)
