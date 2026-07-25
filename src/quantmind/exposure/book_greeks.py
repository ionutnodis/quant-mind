"""Book-level option Greeks (Task A3): pure composition over risk/options.py
(aggregate_greeks, stress_grid) — never re-derives Black-Scholes math. Given a
book's legs (shares + option legs, grouped by underlier) plus each leg's IV
(sourced by the caller from OptionsStore's cached chain) and optional
per-underlier betas, produces per-underlying net Greeks, dollar-delta, and a
SPY-equivalent notional; and a book-level option-sleeve stress grid.

Units note: `aggregate_greeks`'s `delta` is qty*multiplier-scaled but still a
per-$1-underlying-move sensitivity in *shares-equivalent* units (a 100-share
position has delta 100, meaning "$100 of P&L per $1 move" only once you
multiply by spot). `dollar_delta = delta * spot` converts that into an actual
dollar notional of directional exposure — the same convention hedge.py's
`book_value` (qty * price) already uses for its beta-sizing arithmetic.
`spy_equivalent_notional = dollar_delta * beta` is then the SPY-dollar
notional with equivalent index-relative exposure (Engineering Constraint 16 —
beta is estimated upstream; book_greeks only composes).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quantmind.risk.options import OptionLeg, aggregate_greeks, stress_grid


@dataclass(frozen=True)
class BookLeg:
    underlier: str
    qty: float  # shares if not an option; contracts if an option
    is_option: bool
    spot: float  # current spot for this underlier
    r: float = 0.0
    # option-only fields (ignored when is_option is False)
    strike: float | None = None
    expiry_years: float | None = None
    is_call: bool | None = None
    iv: float | None = None
    multiplier: float = 100.0


@dataclass(frozen=True)
class UnderlyingGreeks:
    underlier: str
    spot: float
    delta: float
    gamma: float
    vega: float
    theta: float
    dollar_delta: float
    spy_equivalent_notional: float | None


def _group_by_underlier(legs: list[BookLeg]) -> dict[str, list[BookLeg]]:
    groups: dict[str, list[BookLeg]] = {}
    for leg in legs:
        groups.setdefault(leg.underlier, []).append(leg)
    return groups


def _to_option_leg(leg: BookLeg) -> OptionLeg:
    return OptionLeg(
        qty=leg.qty,
        strike=leg.strike,
        expiry_years=leg.expiry_years,
        is_call=leg.is_call,
        iv=leg.iv,
        multiplier=leg.multiplier,
    )


def compute_book_greeks(
    legs: list[BookLeg], betas: dict[str, float] | None = None
) -> list[UnderlyingGreeks]:
    """One `UnderlyingGreeks` per distinct underlier in `legs`, in first-seen
    order. `betas` maps underlier -> beta vs the app benchmark (e.g. SPY);
    an underlier absent from `betas` (or `betas` omitted) gets
    `spy_equivalent_notional=None` rather than a fabricated 1.0 beta."""
    betas = betas or {}
    results: list[UnderlyingGreeks] = []
    for underlier, group in _group_by_underlier(legs).items():
        spot = group[0].spot
        r = group[0].r
        shares = sum(leg.qty for leg in group if not leg.is_option)
        option_legs = [_to_option_leg(leg) for leg in group if leg.is_option]
        greeks = aggregate_greeks(option_legs, spot=spot, r=r, shares=shares)
        dollar_delta = greeks.delta * spot
        beta = betas.get(underlier)
        spy_notional = dollar_delta * beta if beta is not None else None
        results.append(
            UnderlyingGreeks(
                underlier=underlier,
                spot=spot,
                delta=greeks.delta,
                gamma=greeks.gamma,
                vega=greeks.vega,
                theta=greeks.theta,
                dollar_delta=dollar_delta,
                spy_equivalent_notional=spy_notional,
            )
        )
    return results


def aggregate_book_stress_grid(
    legs: list[BookLeg],
    spot_shocks: tuple[float, ...] = (-0.20, -0.10, -0.05, 0.0, 0.05, 0.10),
    vol_shocks: tuple[float, ...] = (-0.05, 0.0, 0.05, 0.10),
) -> pd.DataFrame:
    """Book-level option-sleeve stress: sums each underlying's full-repricing
    grid (risk/options.py's `stress_grid`) under the SAME relative spot shock
    and SAME absolute vol shift applied simultaneously to every underlier —
    a systemic scenario ("the whole book's underliers move together"), not a
    unified total-book P&L distribution (Engineering Constraint 13's scenario
    framing, extended to a multi-underlier book). An empty book returns an
    all-zero grid over the requested shock axes rather than an empty frame,
    so callers can render it without a special case."""
    groups = _group_by_underlier(legs)
    total: pd.DataFrame | None = None
    for group in groups.values():
        spot = group[0].spot
        r = group[0].r
        shares = sum(leg.qty for leg in group if not leg.is_option)
        option_legs = [_to_option_leg(leg) for leg in group if leg.is_option]
        grid = stress_grid(
            option_legs, spot=spot, r=r, shares=shares, spot_shocks=spot_shocks, vol_shocks=vol_shocks
        )
        total = grid if total is None else total + grid
    if total is None:
        # Matches stress_grid's own orientation: index=vol_shocks, columns=spot_shocks.
        total = pd.DataFrame(0.0, index=list(vol_shocks), columns=list(spot_shocks))
    return total
