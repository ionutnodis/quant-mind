"""Correlation toolkit: matrix, rolling pairwise, and crisis (tail-conditioned)
correlation. Pure functions.

Insufficient data is an explicit error, never a silent NaN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantmind.risk.returns import InsufficientDataError, _require


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.corr()


def rolling_correlation(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    return a.rolling(window).corr(b)


@dataclass(frozen=True)
class CrisisCorrelation:
    """Full-sample vs crisis (worst-market-day) correlation of a return set.

    `crisis_*` figures condition on the `tail_n` worst days of `market`;
    `*_mean_corr` is the average off-diagonal correlation (a diversification
    proxy — it rushes toward 1 in a crisis). The bootstrap CI and `caveat`
    exist because tail samples are small and conditioning on extreme market
    days induces range-restriction bias, so the point estimate alone is not
    decision-grade.
    """

    tail_n: int
    normal_corr: pd.DataFrame
    crisis_corr: pd.DataFrame
    normal_mean_corr: float
    crisis_mean_corr: float
    crisis_mean_corr_ci: tuple[float, float]
    caveat: str


def _mean_offdiagonal(matrix: np.ndarray) -> float:
    """Mean of the upper-triangle (excluding diagonal) — average pairwise
    correlation. NaN entries (a degenerate/zero-variance column) are ignored."""
    p = matrix.shape[0]
    if p < 2:
        return float("nan")
    iu = np.triu_indices(p, k=1)
    vals = matrix[iu]
    vals = vals[np.isfinite(vals)]
    return float(vals.mean()) if len(vals) else float("nan")


def crisis_correlation(
    returns: pd.DataFrame,
    market: pd.Series,
    tail: float = 0.10,
    min_tail: int = 20,
    n_boot: int = 1000,
    seed: int | None = None,
) -> CrisisCorrelation:
    """Correlation of `returns` conditioned on the worst `tail` fraction of
    `market` days, vs the full-sample correlation.

    The worst `floor(n * tail)` market days must number at least `min_tail`
    (small tails give unstable, range-restricted estimates — an explicit error
    is honest where a noisy number is poison). A bootstrap CI over the crisis
    mean pairwise correlation quantifies that instability.
    """
    if not (0.0 < tail < 1.0):
        raise ValueError(f"tail must be in (0, 1), got {tail}")
    _require(min_tail >= 2, "min_tail must be >= 2 to estimate a correlation")

    data = returns.copy()
    data["__mkt__"] = market
    data = data.dropna()
    aligned = data.drop(columns="__mkt__")
    mkt = data["__mkt__"]

    _require(aligned.shape[1] >= 2, "crisis correlation needs >= 2 instruments")
    n = len(aligned)
    k = math.floor(n * tail)
    _require(
        k >= min_tail,
        f"{k} worst-day observations (tail={tail} of {n}) below min_tail={min_tail}",
    )

    worst_idx = mkt.nsmallest(k).index
    crisis_returns = aligned.loc[worst_idx]

    normal_corr = aligned.corr()
    crisis_corr = crisis_returns.corr()
    normal_mean_corr = _mean_offdiagonal(normal_corr.to_numpy())
    crisis_mean_corr = _mean_offdiagonal(crisis_corr.to_numpy())

    crisis_x = crisis_returns.to_numpy()
    rng = np.random.default_rng(seed)
    boot: list[float] = []
    for _ in range(n_boot):
        sample = crisis_x[rng.integers(0, k, size=k)]
        # A resample of identical rows has zero column variance; corrcoef then
        # divides by zero -> NaN, which _mean_offdiagonal drops. Silence the
        # expected warning rather than let it noise the output.
        with np.errstate(invalid="ignore", divide="ignore"):
            c = np.atleast_2d(np.corrcoef(sample, rowvar=False))
        m = _mean_offdiagonal(c)
        if math.isfinite(m):
            boot.append(m)
    if boot:
        lo, hi = (float(v) for v in np.percentile(boot, [2.5, 97.5]))
    else:
        lo = hi = crisis_mean_corr
    ci = (lo, hi)

    caveat = (
        f"Crisis correlation conditions on the {k} worst market days "
        f"(tail={tail:g}); small selected samples plus range-restriction bias "
        "make these directional, not precise — read with the bootstrap CI."
    )

    return CrisisCorrelation(
        tail_n=k,
        normal_corr=normal_corr,
        crisis_corr=crisis_corr,
        normal_mean_corr=normal_mean_corr,
        crisis_mean_corr=crisis_mean_corr,
        crisis_mean_corr_ci=ci,
        caveat=caveat,
    )
