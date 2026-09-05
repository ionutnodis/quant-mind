"""Options domain routes (Task A3): cached chain reads + book Greeks.

GET /api/options/{underlier}/chain: serves OptionsStore's cached snapshot
(never a live IB call — routers read from the store only, Global
Constraints) with an IV smile grouped per expiry and a staleness stamp. No
cached chain -> a structured empty (`missing=True`), never a 404/500: same
"honest empty" posture as routers/macro.py's per-block omission.

POST /api/options/book-greeks: thin composition over the tested pure core
only — `quantmind.exposure.book_greeks` (itself pure, built on
`risk/options.py`'s aggregate_greeks/stress_grid). This router's job is
resolving each leg's IV from the cached chain (option legs) and spot from
cached bars, then handing typed `BookLeg`s to book_greeks; no math beyond
that wiring lives here. Book legs come from `_shared.PositionIn` (A1's shared
contract, already carrying the optional strike/expiry/right/multiplier
fields this endpoint needs) either inline or resolved via `book_ref`
(A1's book.py — `read_book_positions`); exactly one of the two must be given.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown
symbols/legs or bad requests -> structured 422, never a 500.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.api.routers._shared import (
    PositionIn,
    clean,
    collect_currency_assertions,
    iso,
    resolve_symbol_currencies,
)
from quantmind.api.routers.book import (
    read_book,
    read_book_positions,
    validate_pinned_book_scope,
    validate_pinned_instrument_identities,
)
from quantmind.datastore.options_store import OptionsStore, option_chain_freshness
from quantmind.exposure.book_greeks import (
    BookLeg,
    aggregate_book_stress_grid,
    compute_book_greeks,
)

router = APIRouter()

# A chain snapshot is a point-in-time OPRA pull (Task A3's paced sync CLI),
# not continuously refreshed like bars — a few calendar days old (covering a
# weekend/holiday gap) is still "current enough to be honest about"; older
# than that and the page should say so rather than imply live data.
_STALE_AFTER_DAYS = 3

# Repo-wide convention until a rates curve is wired (routers/risk.py's
# alpha_note: "vs SPY, rf=0 until FRED wiring") — the options pricer needs
# SOME r, and 0.0 is the same honest placeholder used elsewhere.
_RISK_FREE_RATE = 0.0


def _options_store(request: Request) -> OptionsStore:
    # OptionsStore is a stateless-construction wrapper around a root Path
    # (datastore/options_store.py) — built lazily from the shared BarStore's
    # root rather than a new app.state attribute, since app.py is off-limits
    # (Global Constraints: never edit api/app.py).
    return OptionsStore(request.app.state.store.root)


class OptionQuoteOut(BaseModel):
    expiry: str
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    multiplier: float


class SmilePoint(BaseModel):
    strike: float
    iv: float | None


class ExpirySmile(BaseModel):
    expiry: str
    points: list[SmilePoint]


class ChainResponse(BaseModel):
    underlier: str
    as_of: str | None
    spot: float | None
    stale: bool
    quotes: list[OptionQuoteOut]
    smile: list[ExpirySmile]
    missing: bool


def _empty_chain_response(underlier: str) -> ChainResponse:
    return ChainResponse(
        underlier=underlier, as_of=None, spot=None, stale=True, quotes=[], smile=[], missing=True
    )


def _build_smile(df: pd.DataFrame) -> list[ExpirySmile]:
    """Per expiry, per strike: average whichever of call/put IV are present
    (both missing -> null, never dropped — the strike axis stays complete so
    a chart can show the gap rather than silently skip a strike)."""
    smiles: list[ExpirySmile] = []
    for expiry, expiry_df in df.groupby("expiry"):
        points: list[SmilePoint] = []
        for strike, strike_df in expiry_df.groupby("strike"):
            ivs = [v for v in strike_df["iv"].tolist() if v is not None and pd.notna(v)]
            iv = float(np.mean(ivs)) if ivs else None
            points.append(SmilePoint(strike=float(strike), iv=clean(iv)))
        points.sort(key=lambda p: p.strike)
        smiles.append(ExpirySmile(expiry=str(expiry), points=points))
    smiles.sort(key=lambda s: s.expiry)
    return smiles


@router.get("/options/{underlier}/chain", response_model=ChainResponse)
def get_chain(underlier: str, request: Request) -> ChainResponse:
    store = _options_store(request)
    if not store.has_chain(underlier):
        return _empty_chain_response(underlier)

    try:
        df, meta = store.read_chain(underlier)
    except (FileNotFoundError, KeyError, OSError, ValueError):
        # TOCTOU-safe fallback: has_chain() and read_chain() are two
        # filesystem calls, never a crash if the file vanished between them.
        return _empty_chain_response(underlier)

    try:
        mapped_con_id = request.app.state.store.read_symbol_map().get(underlier)
    except (OSError, TypeError, ValueError):
        return _empty_chain_response(underlier)
    if mapped_con_id is None or meta.underlier_con_id != mapped_con_id:
        return _empty_chain_response(underlier)

    _, stale = option_chain_freshness(
        meta.as_of,
        date.today(),
        stale_after_business_days=_STALE_AFTER_DAYS,
    )

    quotes = [
        OptionQuoteOut(
            expiry=str(row.expiry),
            strike=float(row.strike),
            right=str(row.right),
            bid=clean(row.bid),
            ask=clean(row.ask),
            iv=clean(row.iv),
            delta=clean(row.delta),
            multiplier=float(row.multiplier),
        )
        for row in df.itertuples()
    ]

    return ChainResponse(
        underlier=underlier,
        as_of=meta.as_of,
        spot=clean(meta.spot),
        stale=stale,
        quotes=quotes,
        smile=_build_smile(df),
        missing=False,
    )


# --- POST /api/options/book-greeks ---


class BookGreeksRequest(BaseModel):
    positions: list[PositionIn] | None = Field(None, max_length=50)
    book_ref: str | None = None
    # Per-underlier beta vs the app benchmark (estimated upstream — routers/
    # risk.py owns beta estimation; this endpoint only consumes it), used for
    # spy_equivalent_notional. Omit an underlier to leave its notional null.
    betas: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "BookGreeksRequest":
        has_positions = self.positions is not None
        has_ref = self.book_ref is not None
        if has_positions == has_ref:
            raise ValueError("exactly one of `positions` or `book_ref` must be provided")
        if has_positions and len(self.positions) == 0:
            raise ValueError("positions must be non-empty when provided")
        return self


class UnderlyingGreeksOut(BaseModel):
    underlier: str
    spot: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    dollar_delta: float | None
    spy_equivalent_notional: float | None


class StressGridOut(BaseModel):
    vol_shocks: list[float]
    spot_shocks: list[float]
    pnl: list[list[float | None]]  # rows = vol_shocks, cols = spot_shocks


class BookGreeksResponse(BaseModel):
    underlyings: list[UnderlyingGreeksOut]
    stress_grid: StressGridOut
    risk_free_rate_note: str
    as_of: str | None


def _spot(store, symbol_map: dict[str, int], symbol: str) -> tuple[float, pd.Timestamp]:
    if symbol not in symbol_map:
        raise HTTPException(422, detail=f"unknown symbol: {symbol!r}")
    try:
        bars, _ = store.read_bars(con_id=symbol_map[symbol], bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    if bars.empty:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached history")
    close = bars["close"]
    last = float(close.iloc[-1])
    if not np.isfinite(last):
        raise HTTPException(422, detail=f"symbol {symbol!r} has a non-finite last close")
    return last, close.index[-1]


def _find_quote_row(
    df: pd.DataFrame,
    expiry: str,
    strike: float,
    right: str,
    con_id: int | None = None,
) -> pd.Series | None:
    mask = (
        (df["expiry"].astype(str) == expiry)
        & (df["right"].astype(str) == right)
        & np.isclose(df["strike"].astype(float), strike, atol=1e-6)
    )
    matched = df.loc[mask]
    if matched.empty:
        return None
    if con_id is not None:
        if "con_id" not in matched.columns:
            return None
        contract_ids = pd.to_numeric(matched["con_id"], errors="coerce")
        exact = matched.loc[contract_ids == con_id]
        return exact.iloc[0] if len(exact) == 1 else None
    return matched.iloc[0] if len(matched) == 1 else None


def _leg_to_book_leg(
    p: PositionIn,
    options_store: OptionsStore,
    spot: float,
    as_of: date,
    underlier_con_id: int,
    contract_con_id: int | None = None,
) -> BookLeg:
    if p.right is None:
        return BookLeg(underlier=p.symbol, qty=p.qty, is_option=False, spot=spot, r=_RISK_FREE_RATE)

    if p.strike is None or p.expiry is None:
        raise HTTPException(
            422,
            detail=f"option leg for {p.symbol!r} needs both strike and expiry (right={p.right!r})",
        )
    if not options_store.has_chain(p.symbol):
        raise HTTPException(
            422, detail=f"no cached option chain for underlier {p.symbol!r} — run options_sync_cli first"
        )
    try:
        chain_df, chain_meta = options_store.read_chain(p.symbol)
    except (FileNotFoundError, KeyError, OSError, ValueError):
        raise HTTPException(
            422,
            detail=f"cached option chain for {p.symbol!r} is invalid — run options_sync_cli first",
        )
    if chain_meta.underlier_con_id != underlier_con_id:
        raise HTTPException(
            422,
            detail=(
                f"cached option chain underlier identity for {p.symbol!r} no longer "
                "matches the instrument map — run options_sync_cli first"
            ),
        )
    row = _find_quote_row(
        chain_df,
        expiry=p.expiry,
        strike=p.strike,
        right=p.right,
        con_id=contract_con_id,
    )
    if row is None:
        raise HTTPException(
            422,
            detail=(
                f"no cached quote for {p.symbol} {p.expiry} {p.strike} {p.right} — "
                "chain may not cover this strike/expiry"
            ),
        )
    iv = row.iv
    if iv is None or not np.isfinite(float(iv)):
        raise HTTPException(
            422, detail=f"cached quote for {p.symbol} {p.expiry} {p.strike} {p.right} has no usable IV"
        )
    try:
        expiry_dt = datetime.strptime(p.expiry, "%Y%m%d").date()
    except ValueError:
        # Unreachable via the API today — PositionIn.expiry (_shared.py)
        # already validates/normalizes to YYYYMMDD on the way in — but cheap
        # insurance against a future caller that constructs a BookLeg
        # bypassing that validator (final-fix-wave finding 4, deferred-minor).
        raise HTTPException(422, detail=f"option leg for {p.symbol} has an unparseable expiry {p.expiry!r}")
    expiry_years = (expiry_dt - as_of).days / 365.25
    if expiry_years <= 0:
        raise HTTPException(
            422, detail=f"option leg for {p.symbol} expires {p.expiry}, which is not in the future"
        )
    multiplier = p.multiplier if p.multiplier is not None else float(row.multiplier)

    return BookLeg(
        underlier=p.symbol,
        qty=p.qty,
        is_option=True,
        spot=spot,
        r=_RISK_FREE_RATE,
        strike=p.strike,
        expiry_years=expiry_years,
        is_call=(p.right == "C"),
        iv=float(iv),
        multiplier=multiplier,
    )


@router.post("/options/book-greeks", response_model=BookGreeksResponse)
def book_greeks(request: Request, req: BookGreeksRequest) -> BookGreeksResponse:
    store = request.app.state.store
    options_store = _options_store(request)

    use_persisted_contract_ids = False
    if req.positions is not None:
        positions = req.positions
    else:
        pinned = read_book(store, req.book_ref)
        validate_pinned_book_scope(request.app.state, pinned)
        positions = read_book_positions(store, req.book_ref)
        validate_pinned_instrument_identities(store, pinned, positions)
        use_persisted_contract_ids = pinned["source"] == "live_ibkr"

    unsupported_security_types = sorted(
        {
            getattr(position, "sec_type", "STK")
            for position in positions
            if getattr(position, "sec_type", "STK") not in {"STK", "OPT"}
        }
    )
    if unsupported_security_types:
        raise HTTPException(
            422,
            detail=(
                "book Greeks do not support security types: "
                f"{unsupported_security_types}"
            ),
        )

    asserted_currencies = collect_currency_assertions(positions)
    currencies = resolve_symbol_currencies(
        store,
        [position.symbol for position in positions],
        asserted=asserted_currencies,
    )
    non_base_currencies = sorted(
        {currency for currency in currencies.values() if currency != request.app.state.base_currency}
    )
    if non_base_currencies:
        raise HTTPException(
            422,
            detail=(
                "cross-currency book Greeks are unavailable until dollar delta, vega, "
                "theta, and stress P&L are normalized leg-by-leg; currencies: "
                f"{non_base_currencies}"
            ),
        )

    symbol_map = store.read_symbol_map()
    as_of = date.today()
    spots: dict[str, float] = {}
    latest_dates: list[pd.Timestamp] = []
    for p in positions:
        if p.symbol not in spots:
            spot, last_date = _spot(store, symbol_map, p.symbol)
            spots[p.symbol] = spot
            latest_dates.append(last_date)

    legs = [
        _leg_to_book_leg(
            p,
            options_store,
            spots[p.symbol],
            as_of,
            underlier_con_id=symbol_map[p.symbol],
            contract_con_id=(
                getattr(p, "con_id", None) if use_persisted_contract_ids else None
            ),
        )
        for p in positions
    ]

    underlyings = compute_book_greeks(legs, betas=req.betas)
    grid = aggregate_book_stress_grid(legs)

    return BookGreeksResponse(
        underlyings=[
            UnderlyingGreeksOut(
                underlier=u.underlier,
                spot=clean(u.spot),
                delta=clean(u.delta),
                gamma=clean(u.gamma),
                vega=clean(u.vega),
                theta=clean(u.theta),
                dollar_delta=clean(u.dollar_delta),
                spy_equivalent_notional=clean(u.spy_equivalent_notional),
            )
            for u in underlyings
        ],
        stress_grid=StressGridOut(
            vol_shocks=[float(v) for v in grid.index],
            spot_shocks=[float(c) for c in grid.columns],
            pnl=[[clean(v) for v in row] for row in grid.to_numpy().tolist()],
        ),
        risk_free_rate_note=f"r={_RISK_FREE_RATE} (rf=0 until FRED wiring, matches routers/risk.py)",
        as_of=iso(max(latest_dates)) if latest_dates else None,
    )
