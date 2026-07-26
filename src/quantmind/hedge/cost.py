"""Hedge cost math (wave-3B "Hedge honest"): pure, picklable functions
(Engineering Constraint 2) — pandas/floats in, floats out; no I/O.

A hedge is never free. Two annualized cost components, both expressed as a
FRACTION OF THE ORIGINAL BOOK'S GROSS per year (the same denominator the
router's ES-overlay convention uses — see routers/hedge.py):

- carry drag: the hedge leg's expected annual return give-up,
  -(hedge_notional * beta_hedge * E[r_bench]) / book_gross. Shorting market
  beta while the benchmark drifts up costs you that drift; a long
  positive-beta overlay shows up as a NEGATIVE drag (an expected tailwind),
  which is displayed as-is rather than floored — honesty over tidiness.
- borrow proxy: shorting (hedge_notional < 0) pays a stock-borrow fee; a
  LONG position in a negative-beta (inverse) fund pays the fund's
  financing/fee stack instead. Both are proxied by one config-style
  constant, BORROW_PROXY_RATE — a labeled PROXY, not a quoted borrow rate
  (real availability/fees are per-instrument and not in the cache).

`protection_per_cost` is the ranking key: delta-ES per unit of annual drag.
It is None (not inf) when the cost is non-positive — a credit/tailwind
candidate has no meaningful "protection per cost" and is ranked by raw
protection instead (router's fallback ordering).
"""

from __future__ import annotations

import pandas as pd

# Config-style constant (labeled a proxy in every response that uses it):
# ~30bp/yr, a general-collateral-ish stock-borrow / inverse-fund fee proxy.
BORROW_PROXY_RATE = 0.0030


def annualized_mean_return(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized arithmetic mean of period returns: mean * periods_per_year.

    The carry-drag estimate wants the expected ANNUAL drift implied by the
    same cached daily bars the rest of the hedge math runs on."""
    if len(returns) == 0:
        raise ValueError("cannot annualize an empty return series")
    return float(returns.mean()) * periods_per_year


def carry_drag_annual(
    hedge_notional: float,
    beta_hedge: float,
    bench_expected_return_annual: float,
    book_gross: float,
) -> float:
    """Expected annual return give-up of the hedge leg, as a fraction of the
    original book's gross: -(N * beta_h * E[r_bench]) / gross. Positive =
    the hedge is expected to cost carry; negative = expected tailwind."""
    if book_gross <= 0:
        raise ValueError("book_gross must be positive")
    return -(hedge_notional * beta_hedge * bench_expected_return_annual) / book_gross


def borrow_proxy_annual(
    hedge_notional: float,
    beta_hedge: float,
    book_gross: float,
    rate: float = BORROW_PROXY_RATE,
) -> float:
    """Borrow/fee proxy: rate * |notional| / gross for a SHORT leg or a LONG
    leg in a negative-beta (inverse) instrument; 0 otherwise. A labeled
    proxy constant — see module docstring."""
    if book_gross <= 0:
        raise ValueError("book_gross must be positive")
    is_short = hedge_notional < 0
    is_long_inverse = hedge_notional > 0 and beta_hedge < 0
    if not (is_short or is_long_inverse):
        return 0.0
    return rate * abs(hedge_notional) / book_gross


def protection_per_cost(protection: float | None, cost_annual: float | None) -> float | None:
    """Ranking key: delta-ES (daily, fraction of gross) per unit of annual
    drag (fraction of gross / year). None unless both inputs exist and the
    cost is strictly positive."""
    if protection is None or cost_annual is None or cost_annual <= 0:
        return None
    return protection / cost_annual
