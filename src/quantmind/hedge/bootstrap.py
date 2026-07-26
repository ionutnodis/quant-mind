"""Paired block-bootstrap CI on delta-ES (wave-3B "Hedge honest").

Global Constraint (wave-3): any bootstrap statistic shows its interval — the
hedge table's delta-ES point estimate therefore ships with this CI.

The block-sampling technique mirrors risk/montecarlo.py's
`simulate_terminal_returns` exactly (same seeded `default_rng`, same
all-randomness-up-front block-start matrix, same non-circular starts in
[0, n_days - block_size]) so the two bootstrap surfaces stay methodologically
identical. It cannot literally CALL that function: `simulate_terminal_returns`
compounds each resampled path into one terminal return, while a delta-ES
replicate needs the whole resampled DAY SERIES (ES is a tail functional of
the days, not of a compounded terminal value). Pure + picklable
(Engineering Constraint 2); deterministic for a given seed.

Pairing matters: each replicate applies the SAME resampled day indices to the
un-hedged and hedged series, so the delta distribution reflects the hedge's
effect, not resampling noise between two independent draws
(tests/test_hedge_math.py pins this with the constant-shift golden).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def delta_es_ci(
    before: pd.Series | np.ndarray,
    after: pd.Series | np.ndarray,
    confidence: float = 0.975,
    ci_level: float = 0.95,
    n_boot: int = 500,
    block_size: int = 5,
    seed: int = 0,
) -> tuple[float, float] | None:
    """(lo, hi) percentile CI on delta-ES = ES(before) - ES(after), both at
    `confidence`, from `n_boot` paired block-bootstrap replicates of the two
    aligned daily return series. None (never an exception) when the series
    are too short for a non-empty ES tail or a full block — the caller
    renders "CI unavailable" honestly instead."""
    b = np.asarray(before, dtype=float)
    a = np.asarray(after, dtype=float)
    if len(b) != len(a):
        raise ValueError(f"before/after must be aligned: {len(b)} vs {len(a)} observations")

    n_days = len(b)
    # epsilon guard mirrors risk/returns.historical_es's floor arithmetic.
    n_tail = math.floor(n_days * (1.0 - confidence) + 1e-9)
    if n_tail < 1 or n_days < block_size:
        return None

    n_blocks = math.ceil(n_days / block_size)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_days - block_size + 1, size=(n_boot, n_blocks))
    offsets = np.arange(block_size)
    day_idx = (starts[:, :, None] + offsets).reshape(n_boot, -1)[:, :n_days]

    # Per-replicate ES: mean of the n_tail smallest resampled days, sign-flipped
    # to a positive loss magnitude (matches risk/returns.historical_es).
    tail_b = np.partition(b[day_idx], n_tail - 1, axis=1)[:, :n_tail]
    tail_a = np.partition(a[day_idx], n_tail - 1, axis=1)[:, :n_tail]
    es_b = -tail_b.mean(axis=1)
    es_a = -tail_a.mean(axis=1)
    delta = es_b - es_a

    alpha = (1.0 - ci_level) / 2.0
    lo, hi = np.quantile(delta, [alpha, 1.0 - alpha])
    return float(lo), float(hi)
