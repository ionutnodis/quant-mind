"""Option hedge structures (wave-3B "Hedge honest"): protective put, put
spread, and collar on one underlier, built from a CACHED chain snapshot
(datastore/options_store.py's frame: expiry/strike/right/bid/ask/iv/delta/
multiplier), sized off risk/options.py's stress_grid, premium expressed as
an annual drag by the caller via `premium_annual_drag`.

Pure composition over the tested pricer only (risk/options.py's bs_price /
stress_grid — never re-derives Black-Scholes math), pure + picklable
(Engineering Constraint 2). No I/O: the caller reads the chain from
OptionsStore and hands the DataFrame in.

Conventions (each one an honesty choice):
- Premiums pay the spread: long legs priced at ASK, short legs at BID. A leg
  without a finite price on the side we'd trade (or without a finite IV) is
  unusable; strike selection then falls to the next-closest usable strike
  rather than fabricating a mid.
- Sizing is off the SAME stress grid the risk page uses: contracts =
  (book sleeve's loss at the shock node) / (per-contract structure payoff at
  that node), both computed with risk.options.stress_grid.
- `structure_daily_pnl` reprices the structure under each historical daily
  underlier return with CONSTANT time-to-expiry and CONSTANT IV — the option
  analogue of the router's constant-notional linear-hedge approximation
  (theta bleed and vol response are carried by the premium-drag cost column
  and the stress grid instead; a full path-dependent reprice is v2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import pandas as pd

from quantmind.risk.options import OptionLeg, bs_price, stress_grid

StructureKind = Literal["protective_put", "put_spread", "collar"]


@dataclass(frozen=True)
class StructureLeg:
    action: Literal["long", "short"]
    strike: float
    right: Literal["C", "P"]
    iv: float
    price: float  # per-share premium at the traded side (ask if long, bid if short)
    multiplier: float


@dataclass(frozen=True)
class OptionStructure:
    kind: StructureKind
    expiry: str  # YYYYMMDD
    expiry_years: float
    legs: tuple[StructureLeg, ...]
    net_premium_per_contract: float  # dollars per 1 structure: sum(+ask long, -bid short) * multiplier


def _parse_expiry(s: str) -> date | None:
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except ValueError:
        return None


def _select_expiry(chain: pd.DataFrame, as_of: date, min_days: int) -> tuple[str, float] | None:
    """Nearest expiry at least `min_days` out (avoids expiring-this-week
    gamma lottery tickets); if none qualify, the farthest still-future one."""
    candidates: list[tuple[int, str]] = []
    for exp in chain["expiry"].astype(str).unique():
        d = _parse_expiry(exp)
        if d is None:
            continue
        days = (d - as_of).days
        if days > 0:
            candidates.append((days, exp))
    if not candidates:
        return None
    eligible = [c for c in candidates if c[0] >= min_days]
    days, exp = min(eligible) if eligible else max(candidates)
    return exp, days / 365.25


def _usable(row: pd.Series, side: Literal["long", "short"]) -> bool:
    """A quote is tradable only if the traded-side price, IV, strike AND
    multiplier are all finite (and positive where zero is nonsense). The
    strike/multiplier checks are fix-round-1 hardening: a corrupt NaN-strike
    row iterated first used to poison the closest-strike comparison (every
    later `dist < nan` is False) and leak NaN into the structure."""
    price = row["ask"] if side == "long" else row["bid"]
    iv = row["iv"]
    strike = row["strike"]
    multiplier = row["multiplier"]
    for value in (price, iv, strike, multiplier):
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            return False
    return True


def _pick_leg(
    df: pd.DataFrame,
    right: str,
    target: float,
    side: Literal["long", "short"],
    strike_below: float | None = None,
    strike_above: float | None = None,
) -> StructureLeg | None:
    """The usable quote of the given right closest to `target`, optionally
    constrained strictly below/above a bound. Unusable quotes (missing traded-
    side price or IV) are skipped, not mid-filled."""
    rows = df[df["right"].astype(str) == right]
    best: tuple[float, pd.Series] | None = None
    for _, row in rows.iterrows():
        strike = float(row["strike"])
        if strike_below is not None and strike >= strike_below:
            continue
        if strike_above is not None and strike <= strike_above:
            continue
        if not _usable(row, side):
            continue
        dist = abs(strike - target)
        if best is None or dist < best[0]:
            best = (dist, row)
    if best is None:
        return None
    row = best[1]
    price = float(row["ask"] if side == "long" else row["bid"])
    return StructureLeg(
        action=side,
        strike=float(row["strike"]),
        right=right,  # type: ignore[arg-type]
        iv=float(row["iv"]),
        price=price,
        multiplier=float(row["multiplier"]),
    )


def _net_premium(legs: tuple[StructureLeg, ...]) -> float:
    total = 0.0
    for leg in legs:
        signed = leg.price if leg.action == "long" else -leg.price
        total += signed * leg.multiplier
    return total


def build_structures(
    chain: pd.DataFrame,
    spot: float,
    as_of: date,
    min_days: int = 20,
    put_otm: float = 0.05,
    spread_width: float = 0.10,
    call_otm: float = 0.05,
) -> tuple[list[OptionStructure], list[str]]:
    """(structures, notes): protective put / put spread / collar from the
    cached chain. Each structure that cannot be built adds a NOTE naming why
    (structured degrade — the router surfaces these, never a 500)."""
    notes: list[str] = []
    if chain is None or len(chain) == 0:
        return [], ["option chain snapshot is empty — no structures can be built"]

    selected = _select_expiry(chain, as_of=as_of, min_days=min_days)
    if selected is None:
        return [], ["no future expiry in the cached chain — no structures can be built"]
    expiry, expiry_years = selected
    df = chain[chain["expiry"].astype(str) == expiry]

    long_put = _pick_leg(df, "P", target=(1.0 - put_otm) * spot, side="long")
    if long_put is None:
        return [], [f"no usable put quote near {(1.0 - put_otm) * 100:.0f}% of spot at {expiry} — no protective structures"]

    structures: list[OptionStructure] = []

    def add(kind: StructureKind, legs: tuple[StructureLeg, ...]) -> None:
        structures.append(
            OptionStructure(
                kind=kind,
                expiry=expiry,
                expiry_years=expiry_years,
                legs=legs,
                net_premium_per_contract=_net_premium(legs),
            )
        )

    add("protective_put", (long_put,))

    short_put = _pick_leg(
        df, "P", target=(1.0 - put_otm - spread_width) * spot, side="short", strike_below=long_put.strike
    )
    if short_put is not None:
        add("put_spread", (long_put, short_put))
    else:
        notes.append(f"put_spread: no usable short-put quote below {long_put.strike:g} at {expiry}")

    short_call = _pick_leg(df, "C", target=(1.0 + call_otm) * spot, side="short", strike_above=spot)
    if short_call is not None:
        add("collar", (long_put, short_call))
    else:
        notes.append(f"collar: no usable short-call quote above spot at {expiry}")

    return structures, notes


def _to_option_legs(structure: OptionStructure, contracts: float) -> list[OptionLeg]:
    return [
        OptionLeg(
            qty=(contracts if leg.action == "long" else -contracts),
            strike=leg.strike,
            expiry_years=structure.expiry_years,
            is_call=(leg.right == "C"),
            iv=leg.iv,
            multiplier=leg.multiplier,
        )
        for leg in structure.legs
    ]


def size_contracts(
    structure: OptionStructure,
    mv_underlier: float,
    spot: float,
    shock: float = -0.20,
    r: float = 0.0,
) -> float | None:
    """Contracts so the structure's payoff at the `shock` stress node offsets
    the underlier sleeve's loss there — BOTH sides computed with
    risk.options.stress_grid (the existing grid, per spec). None when the
    sleeve doesn't lose at the node or the structure doesn't pay there."""
    book_loss = float(
        stress_grid([], spot=spot, r=r, shares=mv_underlier / spot, spot_shocks=(shock,), vol_shocks=(0.0,)).iloc[0, 0]
    )
    if book_loss >= 0:
        return None
    payoff = float(
        stress_grid(_to_option_legs(structure, 1.0), spot=spot, r=r, spot_shocks=(shock,), vol_shocks=(0.0,)).iloc[0, 0]
    )
    if payoff <= 0:
        return None
    return -book_loss / payoff


def structure_daily_pnl(
    structure: OptionStructure,
    contracts: float,
    spot: float,
    underlier_returns: pd.Series,
    r: float = 0.0,
) -> pd.Series:
    """Dollar P&L of `contracts` structures under each historical daily
    underlier return: full bs_price reprice at spot*(1+ret) minus the base
    price, constant T and IV (see module docstring's approximation note)."""
    legs = _to_option_legs(structure, contracts)
    base = sum(
        leg.qty * leg.multiplier * bs_price(spot, leg.strike, leg.expiry_years, r, leg.iv, leg.is_call)
        for leg in legs
    )
    values = []
    for ret in underlier_returns.to_numpy(dtype=float):
        shocked = sum(
            leg.qty
            * leg.multiplier
            * bs_price(spot * (1.0 + ret), leg.strike, leg.expiry_years, r, leg.iv, leg.is_call)
            for leg in legs
        )
        values.append(shocked - base)
    return pd.Series(values, index=underlier_returns.index)


def premium_annual_drag(
    net_premium_per_contract: float,
    contracts: float,
    book_gross: float,
    expiry_years: float,
) -> float:
    """The structure's net premium as an ANNUAL drag, fraction of the
    original book's gross: premium * contracts / gross / T. A credit
    structure (negative net premium, e.g. some collars) yields a negative
    drag — reported as-is; the ranking key treats non-positive cost as
    un-rankable (cost.protection_per_cost)."""
    if book_gross <= 0:
        raise ValueError("book_gross must be positive")
    if expiry_years <= 0:
        raise ValueError("expiry_years must be positive")
    return net_premium_per_contract * contracts / book_gross / expiry_years
