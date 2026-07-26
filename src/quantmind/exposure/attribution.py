"""Core-vs-overlay P&L attribution (Task B1 — the product's identity number:
is the overlay adding alpha?). Pure decomposition of the book's DAILY DOLLAR
P&L into two pieces:

  total_pnl(t)   = book_value * book_return(t)
  core_pnl(t)    = book_value * beta * bench_return(t)      -- "the beta core's job"
  overlay_pnl(t) = total_pnl(t) - core_pnl(t)                -- residual: "the overlay's job"

This is the CAPM decomposition (routers/risk.py's rolling_beta/rolling_alpha
single-factor model, Engineering Constraint 4: alpha is Jensen's alpha) lifted
from returns-space into book-dollar-space: r_book = beta*r_bench + residual,
so book_value*r_book = book_value*beta*r_bench + book_value*residual exactly
(core_pnl + overlay_pnl == total_pnl at every observation, by construction —
no separate re-derivation, no drift).

Pure (Engineering Constraint 2): pandas/dataclasses in and out. `beta` and the
aligned return series are estimated upstream (routers/portfolio.py, reusing
quantmind.risk.returns.rolling_beta — the same beta estimator routers/risk.py
and routers/hedge.py already use); this module only composes the arithmetic
and never re-estimates beta itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


class InsufficientDataError(ValueError):
    pass


@dataclass(frozen=True)
class PnlSplitSummary:
    n_obs: int
    total_pnl: float
    core_pnl: float
    overlay_pnl: float
    # None (never a fabricated/divide-by-zero NaN) when total_pnl nets to
    # exactly zero over the window — a share of zero is undefined, not 0%.
    core_share: float | None
    overlay_share: float | None


def decompose_book_pnl(
    book_returns: pd.Series,
    bench_returns: pd.Series,
    beta: float,
    book_value: float,
) -> pd.DataFrame:
    """Per-observation core/overlay dollar P&L, inner-joined on the two
    series' index (only dates both cover contribute — matches routers/risk.py
    and routers/hedge.py's alignment convention elsewhere in the repo).
    Raises InsufficientDataError (never returns a silently empty frame) when
    the two series share no observations."""
    # sort=True pins today's union-index sorting explicitly (pandas 4 flips
    # concat's default to sort=False; silences Pandas4Warning — F11).
    aligned = pd.concat({"book": book_returns, "bench": bench_returns}, axis=1, sort=True).dropna()
    if aligned.empty:
        raise InsufficientDataError("no overlapping book/benchmark return observations")

    total_pnl = aligned["book"] * book_value
    core_pnl = beta * aligned["bench"] * book_value
    overlay_pnl = total_pnl - core_pnl
    return pd.DataFrame(
        {"total_pnl": total_pnl, "core_pnl": core_pnl, "overlay_pnl": overlay_pnl},
        index=aligned.index,
    )


def summarize_pnl_split(decomposed: pd.DataFrame) -> PnlSplitSummary:
    """Window totals + shares from `decompose_book_pnl`'s output. Raises
    InsufficientDataError on an empty frame (a caller should never summarize
    a decomposition it never should have produced)."""
    if decomposed.empty:
        raise InsufficientDataError("cannot summarize an empty P&L decomposition")

    total = float(decomposed["total_pnl"].sum())
    core = float(decomposed["core_pnl"].sum())
    overlay = float(decomposed["overlay_pnl"].sum())
    core_share = core / total if total != 0.0 else None
    overlay_share = overlay / total if total != 0.0 else None

    return PnlSplitSummary(
        n_obs=len(decomposed),
        total_pnl=total,
        core_pnl=core,
        overlay_pnl=overlay,
        core_share=core_share,
        overlay_share=overlay_share,
    )
