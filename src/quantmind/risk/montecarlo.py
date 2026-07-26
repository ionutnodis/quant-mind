"""Block-bootstrap Monte Carlo over joint asset returns.

Sampling whole blocks of rows preserves cross-asset correlation (and short-range
autocorrelation) without assuming a distribution. All randomness is drawn
up-front so chunking never changes results (Engineering Constraint 10), and the
function is pure + picklable for worker-process dispatch (Constraint 2).

    returns (days x assets) ──┐
                              ├─> portfolio daily returns r1 = R @ w
    block starts (paths x B) ─┘        │
                                       ▼
              per-path day indices ──> compound ──> terminal returns (n_paths,)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def simulate_terminal_returns(
    returns: pd.DataFrame,
    weights: np.ndarray,
    n_paths: int,
    horizon: int,
    block_size: int = 5,
    seed: int | None = None,
    chunk_size: int = 5000,
) -> np.ndarray:
    """Terminal portfolio returns over `horizon` days across `n_paths` bootstrap paths."""
    r1 = returns.to_numpy() @ np.asarray(weights, dtype=float)  # portfolio daily returns
    n_days = len(r1)
    n_blocks = math.ceil(horizon / block_size)

    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_days - block_size + 1, size=(n_paths, n_blocks))

    offsets = np.arange(block_size)
    out = np.empty(n_paths, dtype=float)
    for lo in range(0, n_paths, chunk_size):
        hi = min(lo + chunk_size, n_paths)
        day_idx = (starts[lo:hi, :, None] + offsets).reshape(hi - lo, -1)[:, :horizon]
        daily = r1[day_idx]
        out[lo:hi] = np.prod(1.0 + daily, axis=1) - 1.0
    return out
