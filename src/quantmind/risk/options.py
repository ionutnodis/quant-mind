"""Options-aware risk layer: Black-Scholes repricer, Greeks aggregation, stress grids.

Honest ES scoping (Engineering Constraint 13): this module provides the options
sleeve's scenario view — grid P&L via full repricing under spot/vol shocks —
not a unified total-book distribution (that is the v2 roadmap).

The pricer sits behind plain functions so a QuantLib American/dividend repricer
can replace `bs_price` per Open Question 7 without touching aggregation or grids.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import pandas as pd
from scipy.stats import norm

_MIN_VOLATILITY = 1e-6


@dataclass(frozen=True)
class OptionLeg:
    qty: float  # contracts, signed
    strike: float
    expiry_years: float
    is_call: bool
    iv: float
    multiplier: float = 100.0


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    vega: float
    theta: float


def _d1_d2(spot: float, strike: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    if not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("volatility must be finite and positive")
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    return d1, d1 - sigma * math.sqrt(t)


def bs_price(spot: float, strike: float, t: float, r: float, sigma: float, is_call: bool) -> float:
    d1, d2 = _d1_d2(spot, strike, t, r, sigma)
    if is_call:
        return spot * norm.cdf(d1) - strike * math.exp(-r * t) * norm.cdf(d2)
    return strike * math.exp(-r * t) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_greeks(spot: float, strike: float, t: float, r: float, sigma: float, is_call: bool) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, t, r, sigma)
    delta = norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0
    gamma = norm.pdf(d1) / (spot * sigma * math.sqrt(t))
    vega = spot * norm.pdf(d1) * math.sqrt(t)  # per 1.00 change in vol
    theta_core = -spot * norm.pdf(d1) * sigma / (2 * math.sqrt(t))
    if is_call:
        theta = theta_core - r * strike * math.exp(-r * t) * norm.cdf(d2)
    else:
        theta = theta_core + r * strike * math.exp(-r * t) * norm.cdf(-d2)
    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta)


def aggregate_greeks(legs: Sequence[OptionLeg], spot: float, r: float, shares: float = 0.0) -> Greeks:
    """Position-level Greeks: sum of qty * multiplier * per-unit greek, plus share delta."""
    delta, gamma, vega, theta = shares, 0.0, 0.0, 0.0
    for leg in legs:
        g = bs_greeks(spot, leg.strike, leg.expiry_years, r, leg.iv, leg.is_call)
        scale = leg.qty * leg.multiplier
        delta += scale * g.delta
        gamma += scale * g.gamma
        vega += scale * g.vega
        theta += scale * g.theta
    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta)


def stress_grid(
    legs: Sequence[OptionLeg],
    spot: float,
    r: float,
    shares: float = 0.0,
    spot_shocks: Sequence[float] = (-0.20, -0.10, -0.05, 0.0, 0.05, 0.10),
    vol_shocks: Sequence[float] = (-0.05, 0.0, 0.05, 0.10),
) -> pd.DataFrame:
    """Full-repricing P&L grid: rows = vol shocks (absolute points), cols = spot shocks (relative)."""

    def book_value(s: float, vol_shift: float) -> float:
        value = shares * s
        for leg in legs:
            shocked_iv = max(leg.iv + vol_shift, _MIN_VOLATILITY)
            value += (
                leg.qty
                * leg.multiplier
                * bs_price(s, leg.strike, leg.expiry_years, r, shocked_iv, leg.is_call)
            )
        return value

    base = book_value(spot, 0.0)
    data = {
        vs: [book_value(spot * (1 + ss), vs) - base for ss in spot_shocks] for vs in vol_shocks
    }
    return pd.DataFrame(data, index=list(spot_shocks), columns=list(vol_shocks)).T
