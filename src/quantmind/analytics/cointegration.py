"""Cointegration diagnostics (Engineering Constraint 12: supporting evidence for
hedge candidates, NOT the ranking engine — ranking is objective-based)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import coint


@dataclass(frozen=True)
class CointResult:
    pvalue: float
    hedge_ratio: float  # OLS beta of y on x: the spread is y - hedge_ratio * x
    # OLS standard error of the hedge ratio (wave-3B: uncertainty is
    # displayed). Caveat honestly noted: in the cointegrated case the
    # first-stage OLS estimator is superconsistent and the classical SE is an
    # approximation, not an exact sampling distribution — it is shown as a
    # scale of uncertainty, never as a formal test statistic.
    hedge_ratio_se: float

    def is_cointegrated(self, threshold: float = 0.05) -> bool:
        return self.pvalue < threshold


def engle_granger(y: pd.Series, x: pd.Series) -> CointResult:
    """Engle-Granger two-step test of y against x on their common index."""
    df = pd.concat({"y": y, "x": x}, axis=1).dropna()
    _, pvalue, _ = coint(df["y"], df["x"])
    ols = sm.OLS(df["y"].to_numpy(), sm.add_constant(df["x"].to_numpy())).fit()
    return CointResult(
        pvalue=float(pvalue),
        hedge_ratio=float(ols.params[1]),
        hedge_ratio_se=float(ols.bse[1]),
    )
