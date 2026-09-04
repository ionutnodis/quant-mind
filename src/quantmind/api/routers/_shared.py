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

from quantmind.fx import FxConversionUnavailable, FxConverter

T = TypeVar("T", bound=Sequence)


class FxEvidenceOut(BaseModel):
    """Shared public evidence shape for every base-currency analysis."""

    status: Literal["identity", "converted", "incomplete"]
    base_currency: str
    source: str | None
    as_of: str | None
    fetched_at: str | None
    missing_currencies: list[str]
    note: str


def complete_fx_evidence(
    converter: FxConverter,
    *,
    base_currency: str,
) -> FxEvidenceOut:
    converted = converter.source != "identity"
    return FxEvidenceOut(
        status="converted" if converted else "identity",
        base_currency=base_currency,
        source=converter.source if converted else None,
        as_of=converter.as_of,
        fetched_at=converter.fetched_at or None,
        missing_currencies=[],
        note=(
            f"Prices are normalized to {base_currency} with dated "
            f"{converter.source} evidence."
            if converted
            else f"All analytical prices are denominated in {base_currency}."
        ),
    )


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
    first/last-ish spread rather than truncating. Works for anything that
    supports `len()` and `[::step]` slicing — a `list` (risk.py's
    `BetaPoint` points) or a `pd.Series` (macro.py's named series) alike."""
    if len(seq) <= max_points:
        return seq
    step = math.ceil(len(seq) / max_points)
    return seq[::step]


def read_close_series(store, con_id: int, symbol: str, years: int) -> pd.Series:
    """Cached daily close series for `con_id`, tail-clipped to `years` (0 =
    full history). Missing bars or an empty result -> a structured 422 naming
    the symbol, never a 500 (pattern shared by whatif.py and hedge.py)."""
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    series = bars["close"]
    if years > 0:
        series = series.iloc[-(years * 252):]
    if series.empty:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached history")
    return series


def resolve_symbol_currencies(
    store,
    symbols: Sequence[str],
    *,
    asserted: dict[str, str | None] | None = None,
) -> dict[str, str]:
    """Resolve authoritative quote currencies for analytical inputs.

    A posted currency is a consistency assertion, not a way to override the
    instrument master.  Pinned broker books may carry the only known value,
    so an assertion is accepted when cached metadata is absent.
    """

    asserted = asserted or {}
    metadata = read_instrument_metadata_map(store)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for symbol in dict.fromkeys(symbols):
        master_value = (metadata.get(symbol) or {}).get("currency")
        master = str(master_value).strip().upper() if master_value else None
        asserted_value = asserted.get(symbol)
        claim = str(asserted_value).strip().upper() if asserted_value else None
        if master is not None and claim is not None and master != claim:
            raise HTTPException(
                422,
                detail=(
                    f"currency assertion for {symbol!r} ({claim}) conflicts with "
                    f"instrument metadata ({master})"
                ),
            )
        currency = master or claim
        if currency is None:
            missing.append(symbol)
        else:
            resolved[symbol] = currency
    if missing:
        raise HTTPException(
            422,
            detail=f"quote currency is unknown for symbols: {sorted(missing)}; sync metadata first",
        )
    return resolved


def read_instrument_metadata_map(store) -> dict[str, dict]:
    """Read the instrument master as a named analytical data dependency."""

    try:
        return store.read_all_instrument_metadata()
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            422,
            detail="instrument metadata cache is corrupt; run sync to rebuild it",
        ) from exc


def collect_currency_assertions(
    positions: Sequence[object],
) -> dict[str, str]:
    """Build one normalized, conflict-checked currency claim per symbol."""

    asserted: dict[str, str] = {}
    for position in positions:
        symbol = str(getattr(position, "symbol"))
        raw_currency = getattr(position, "currency", None)
        if not raw_currency:
            continue
        currency = str(raw_currency).strip().upper()
        previous = asserted.get(symbol)
        if previous is not None and previous != currency:
            raise HTTPException(
                422,
                detail=f"conflicting currency assertions for {symbol!r}",
            )
        asserted[symbol] = currency
    return asserted


def load_base_currency_series(
    store,
    symbol_map: dict[str, int],
    symbols: Sequence[str],
    *,
    years: int,
    base_currency: str,
    asserted_currencies: dict[str, str | None] | None = None,
) -> tuple[dict[str, pd.Series], dict[str, str], FxConverter]:
    """Load local closes once and normalize them before any return math."""

    ordered = list(dict.fromkeys(symbols))
    currencies = resolve_symbol_currencies(
        store, ordered, asserted=asserted_currencies
    )
    try:
        converter = FxConverter.from_store(
            store,
            base_currency=base_currency,
            currencies=set(currencies.values()),
        )
    except (FxConversionUnavailable, ValueError) as exc:
        needed = sorted({value for value in currencies.values() if value != base_currency})
        raise HTTPException(
            422,
            detail=(
                f"dated FX normalization to {base_currency} is unavailable for {needed}: "
                f"{exc}; run sync"
            ),
        ) from exc

    output: dict[str, pd.Series] = {}
    for symbol in ordered:
        local = read_close_series(store, symbol_map[symbol], symbol, years)
        try:
            output[symbol] = converter.convert_series(local, currencies[symbol])
        except FxConversionUnavailable as exc:
            raise HTTPException(
                422,
                detail=(
                    f"dated FX normalization for {symbol!r} ({currencies[symbol]}) "
                    f"is unavailable: {exc}; run sync"
                ),
            ) from exc
    return output, currencies, converter


class PositionIn(BaseModel):
    """A book leg: qty != 0 shares/contracts of `symbol`. See module
    docstring for the option-leg fields' optionality/defaulting convention."""

    symbol: str = Field(..., min_length=1)
    qty: float
    # Optional option-leg fields (wave-3 Task A3 coordination point). Legacy
    # linear routes inspect them only to fail closed until contract repricing
    # exists; book-greeks consumes the full contract.
    strike: float | None = None
    expiry: str | None = None
    right: Literal["C", "P"] | None = None
    multiplier: float | None = None
    currency: str | None = None
    exchange: str | None = None

    @field_validator("qty")
    @classmethod
    def _qty_nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("qty must be nonzero")
        return v

    @field_validator("qty", "strike", "multiplier")
    @classmethod
    def _numeric_terms_are_finite(cls, v: float | None, info) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError(f"{info.field_name} must be finite")
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


def refuse_unsupported_contract_legs(
    positions: Sequence[PositionIn], *, route_name: str
) -> None:
    """Stop legacy share-return routes from silently treating contracts as stock.

    These routes do not consume option terms or multipliers. Until they use a
    validated contract pricer, any option-shaped leg or non-unit multiplier
    makes the whole result unsupported rather than partially correct.
    """
    unsupported = sorted(
        {
            p.symbol
            for p in positions
            if getattr(p, "sec_type", "STK") != "STK"
            or p.right is not None
            or p.strike is not None
            or p.expiry is not None
            or (p.multiplier is not None and not math.isclose(p.multiplier, 1.0))
        }
    )
    if unsupported:
        raise HTTPException(
            422,
            detail=(
                f"{route_name} cannot value option/non-stock contracts or non-unit-multiplier legs "
                f"with the legacy share-return model: {unsupported}"
            ),
        )


def weighted_portfolio_returns(returns: pd.DataFrame, symbols: list[str], weights: np.ndarray) -> pd.Series:
    """Weighted sum of aligned per-symbol simple returns: `symbols[i]`'s
    column of `returns` gets `weights[i]`. `symbols` may repeat (whatif.py's
    per-position call) — a repeated symbol simply reuses the same column, and
    its weights add, which is the correct portfolio-return contribution."""
    values = np.column_stack([returns[s].to_numpy() for s in symbols])
    return pd.Series(values @ weights, index=returns.index)
