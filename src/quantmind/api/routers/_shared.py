"""Shared helpers extracted from the wave-2 routers (pre-wave-3 consolidation
pass, TODOS.md "Pre-wave-3 consolidation pass"): `clean`/`iso`/`downsample`
were duplicated 7/4/2 times across portfolio.py, risk.py, lab.py, macro.py,
whatif.py and hedge.py (a NaN-policy change needed 7 edits); `read_close_series`
was duplicated in whatif.py and hedge.py; the qty-nonzero position model was
duplicated as whatif.py's `PositionIn` and hedge.py's `BookPositionIn`.

`weighted_portfolio_returns` is the aligned weighted-returns block shared by
whatif.py and hedge.py: given an already price-aligned `returns` DataFrame and
a parallel (symbols, weights) pair, it is the weighted sum of each symbol's
column — whatif.py calls it with one entry per POSITION (so a duplicate
symbol across two positions reuses the same aligned column, correct since the
weights simply add), hedge.py calls it with one entry per already-netted
SYMBOL. Both prior inline implementations (`position_returns @ weights_arr`
in whatif.py, `returns[symbols].to_numpy() @ weights_arr` in hedge.py) are
the identical linear-algebra operation; this is golden-tested against
hand-computed values in tests/test_router_shared.py, and whatif/hedge's own
existing end-to-end tests (beta-of-100%-SPY-book ~= 1, hedge sizing formula)
continue to pass unchanged after the swap, which is the behavior-preservation
check Beck's refactor discipline calls for before the old inline blocks were
deleted.

`PositionIn` gained OPTIONAL option-leg fields (strike, expiry, right,
multiplier) for wave-3 Task A3's `/api/options/book-greeks` endpoint, which
needs the exact same book-leg contract whatif/hedge/book already use — this
lets A3 extend the request shape (optional fields only, additive) without
ever needing to edit this file. `multiplier` deliberately has no Pydantic
default of 100 baked in: a bare `PositionIn` is used for plain equity/ETF
legs everywhere in whatif/hedge/book today, and defaulting multiplier to 100
at the model level would silently 100x a stock leg's implied notional
wherever a consumer reads `.multiplier`. Consumers that need "unspecified
multiplier on an option leg means 100" (book.py's Position construction;
A3's book_greeks) apply that convention explicitly: `p.multiplier if
p.multiplier is not None else (100.0 if p.right else 1.0)`.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Sequence, TypeVar

import numpy as np
import pandas as pd
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from quantmind.fx import FxConverter, fx_pair

T = TypeVar("T", bound=Sequence)


def clean(x: float | None) -> float | None:
    """NaN/Inf -> null (repo-wide serialization policy): never let a
    non-finite float reach a JSON response."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf):
        return None
    return xf


def iso(ts: pd.Timestamp) -> str:
    """UTC ISO-8601 Z-suffixed timestamp (repo-wide serialization policy)."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def downsample(seq: T, max_points: int) -> T:
    """Step-slice `seq` down to at most `max_points` elements, preserving the
    first AND the TRUE last element (batch-2 final review item 7f: `[::step]`
    silently dropped the most recent point — the as-of anchor — unless
    (len-1) % step == 0). Works for anything that supports `len()` and
    `[::step]` slicing — a `list` (risk.py's `BetaPoint` points) or a
    `pd.Series` (macro.py's named series, lab.py's pair spread) alike."""
    if len(seq) <= max_points:
        return seq
    step = math.ceil(len(seq) / max_points)
    sampled = seq[::step]
    if (len(seq) - 1) % step == 0:
        return sampled  # stride already lands on the last element
    if len(sampled) >= max_points:
        # Make room for the true last element by dropping the last interior
        # sample — the endpoints matter more than one interior point.
        sampled = sampled[:-1]
    tail = seq[len(seq) - 1:]
    if isinstance(sampled, pd.Series):
        return pd.concat([sampled, tail])
    return sampled + tail


def read_close_series(store, con_id: int, symbol: str, years: int) -> pd.Series:
    """Cached daily close series for `con_id`, tail-clipped to `years` (0 =
    full history). Missing bars or an empty result -> a structured 422 naming
    the symbol, never a 500 (pattern shared by whatif.py and hedge.py)."""
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    series = bars["close"]
    if years > 0:
        series = series.iloc[-(years * 252):]
    if series.empty:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached history")
    return series


def load_fx_converter(store, base: str) -> FxConverter:
    """FX-aware valuation (TODOS 2026-07-27): build the currency→`base`
    converter every router prices with. For each distinct currency in the
    stored instrument metadata (≠ base), read the cached FX_{pair} series
    (written by sources/sync.sync_fx_bars) and orient its latest close via
    fx_pair's invert flag. A missing/empty/garbage series simply leaves the
    currency ABSENT from `rates` — FxConverter.convert then returns None and
    callers degrade honestly (exclusion + note, or a named 422), never a
    silently unconverted native amount. `as_of` is the OLDEST of the loaded
    rates' last dates (conservative staleness label)."""
    currencies = {
        cur
        for md in store.read_all_instrument_metadata().values()
        if (cur := md.get("currency")) and cur != base
    }
    rates: dict[str, float] = {}
    last_dates: list[pd.Timestamp] = []
    for cur in sorted(currencies):
        pair, invert = fx_pair(cur, base)
        try:
            series = store.read_series(f"FX_{pair}")
        except FileNotFoundError:
            continue
        series = series.dropna()
        series = series[np.isfinite(series) & (series > 0)]
        if series.empty:
            continue
        close = float(series.iloc[-1])
        rates[cur] = 1.0 / close if invert else close
        last_dates.append(pd.Timestamp(series.index[-1]))
    as_of = str(min(last_dates).date()) if last_dates else None
    return FxConverter(base=base, rates=rates, as_of=as_of)


def symbol_currencies(store, symbols: Sequence[str]) -> dict[str, str | None]:
    """Native quote currency per symbol from cached instrument metadata;
    None when the metadata doesn't exist or carries no currency (absence of
    proof is not proof of one currency)."""
    all_md = store.read_all_instrument_metadata()
    return {s: (all_md.get(s) or {}).get("currency") for s in symbols}


def fx_rates_for(store, symbols: Sequence[str], converter: FxConverter) -> dict[str, float]:
    """Per-symbol multiplier to `converter.base` for the compute routers
    (whatif/hedge/lab): 1.0 for the base itself AND for symbols with no
    cached currency metadata (the pre-FX behavior — a hypothetical book of
    unsynced symbols keeps valuing natively rather than refusing). A KNOWN
    non-base currency with no cached rate is a named 422 — computing a
    book's gross/weights off mixed currencies is exactly the silent bias
    this pass removes."""
    currencies = symbol_currencies(store, symbols)
    rates: dict[str, float] = {}
    missing: dict[str, list[str]] = {}
    for sym, cur in currencies.items():
        if cur is None or cur == converter.base:
            rates[sym] = 1.0
            continue
        rate = converter.rates.get(cur)
        if rate is None:
            missing.setdefault(cur, []).append(sym)
        else:
            rates[sym] = rate
    if missing:
        curs = ", ".join(sorted(missing))
        syms = sorted({s for group in missing.values() for s in group})
        raise HTTPException(
            422,
            detail=(
                f"no cached FX rate for {curs} (needed to value {syms} in "
                f"{converter.base}) — run sync to cache the pair"
            ),
        )
    return rates


def fx_conversion_note(converter: FxConverter, currencies: Sequence[str | None]) -> str | None:
    """The honest-disclosure line for a converted book: names the base,
    each converted currency's pair, and the conservative rate as_of. None
    when nothing was converted (single-currency book in its own base)."""
    converted = sorted(
        {c for c in currencies if c and c != converter.base and c in converter.rates}
    )
    if not converted:
        return None
    legs = ", ".join(
        f"{cur} legs converted at cached {fx_pair(cur, converter.base)[0]}" for cur in converted
    )
    return f"valued in {converter.base}; {legs} ({converter.as_of})"


class PositionIn(BaseModel):
    """A book leg: qty != 0 shares/contracts of `symbol`. See module
    docstring for the option-leg fields' optionality/defaulting convention."""

    symbol: str = Field(..., min_length=1)
    qty: float
    # Optional option-leg fields (wave-3 Task A3 coordination point) — unused
    # by whatif/hedge/book today, present so A3's book-greeks endpoint never
    # needs to edit this file to extend the shared request contract.
    strike: float | None = None
    expiry: str | None = None
    right: Literal["C", "P"] | None = None
    multiplier: float | None = None

    @field_validator("qty")
    @classmethod
    def _qty_nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("qty must be nonzero")
        return v

    @field_validator("expiry")
    @classmethod
    def _normalize_expiry(cls, v: str | None) -> str | None:
        """Accept either options.py's wire format (YYYYMMDD) or the ISO form
        (YYYY-MM-DD) book.py's own tests happened to use — two conventions
        that silently diverged across the wave-3 routers (final-fix-wave
        finding 4) — and normalize to YYYYMMDD everywhere downstream."""
        if v is None:
            return None
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(v, fmt).strftime("%Y%m%d")
            except ValueError:
                continue
        raise ValueError(f"expiry must be YYYYMMDD or YYYY-MM-DD, got {v!r}")


# The declared delta-one approximation for option legs in the returns-based
# engines (whatif/hedge/lab book-regression) — surfaced in each response's
# `notes`, never silent (Batch-2 final review item 2).
DELTA_ONE_OPTION_NOTE = (
    "Option legs are priced as delta-one underlier notional (qty x multiplier x spot) "
    "in this returns-based engine — a declared approximation; Greeks-aware option risk "
    "lives in the options layer (book-greeks)."
)


def _effective_multiplier(p: "PositionIn") -> float:
    """PositionIn's convention (module docstring): multiplier has no baked-in
    default so a bare equity leg is never silently 100x'd; an option leg
    (right set) defaults to the standard 100, anything else to 1.0 (a plain
    share). Moved from whatif.py (Batch-2 final review item 2) so hedge/lab's
    book pricing shares the exact same convention."""
    if p.multiplier is not None:
        return p.multiplier
    return 100.0 if p.right is not None else 1.0


def _validate_option_legs(positions: list["PositionIn"], origin: str) -> None:
    """Batch-2 final review item 1 (moved unchanged from whatif.py so the
    guard is shared): strike/expiry/right are ALL-or-NONE on every leg, on
    BOTH the inline and book_ref paths (parity with book.py's honest-refusal
    guard for pinned OPT legs). A leg with `right` but no strike/expiry
    cannot be priced (and used to slip through inline, silently valued at
    100x underlier notional); a leg with strike/expiry but no `right` used
    to key as a phantom separate STK line in the trade ticket. Refuse both
    with a named 422, never a silent mispricing."""
    for p in positions:
        fields = {"strike": p.strike, "expiry": p.expiry, "right": p.right}
        given = [k for k, v in fields.items() if v is not None]
        if given and len(given) < len(fields):
            missing = [k for k, v in fields.items() if v is None]
            raise HTTPException(
                422,
                detail=(
                    f"{origin} leg {p.symbol!r} has a partial option descriptor "
                    f"(has {'/'.join(given)}, missing {'/'.join(missing)}) — "
                    "strike/expiry/right must be given together (all or none)"
                ),
            )


def weighted_portfolio_returns(returns: pd.DataFrame, symbols: list[str], weights: np.ndarray) -> pd.Series:
    """Weighted sum of aligned per-symbol simple returns: `symbols[i]`'s
    column of `returns` gets `weights[i]`. `symbols` may repeat (whatif.py's
    per-position call) — a repeated symbol simply reuses the same column, and
    its weights add, which is the correct portfolio-return contribution."""
    values = np.column_stack([returns[s].to_numpy() for s in symbols])
    return pd.Series(values @ weights, index=returns.index)
