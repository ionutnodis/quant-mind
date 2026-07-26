"""Tail-conditional protection (wave-3B "Hedge honest"): on the worst-decile
BENCHMARK days in the window, what did the book do with vs without the hedge?

This answers the question ES compresses away: "when the market has its bad
days, does this hedge actually show up?" Pure + picklable (Engineering
Constraint 2): aligned daily return series in, a small stats record out.
Horizon: DAILY means over the selected days — the router labels it so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TailStats:
    n_days: int  # how many worst-decile benchmark days were in the window
    mean_book: float  # mean daily un-hedged book return on those days
    mean_hedged: float  # mean daily hedged book return on those days


def worst_decile_tail(
    book: pd.Series,
    hedged: pd.Series,
    bench: pd.Series,
    decile: float = 0.10,
) -> TailStats | None:
    """Inner-join the three series on their index, take the floor(n * decile)
    days with the LOWEST benchmark return, and report the book's mean daily
    return on those days with and without the hedge. None (never an
    exception) when the joined window is too short for a non-empty decile."""
    aligned = pd.concat({"book": book, "hedged": hedged, "bench": bench}, axis=1).dropna()
    n = len(aligned)
    # epsilon guard mirrors risk/returns.historical_es's floor arithmetic.
    n_tail = math.floor(n * decile + 1e-9)
    if n_tail < 1:
        return None
    worst = aligned.nsmallest(n_tail, "bench")
    return TailStats(
        n_days=n_tail,
        mean_book=float(worst["book"].mean()),
        mean_hedged=float(worst["hedged"].mean()),
    )
