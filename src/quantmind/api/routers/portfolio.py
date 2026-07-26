"""Portfolio domain routes — the truth about the book (DESIGN.md: "the truth
about the book"). Thin wrapper over the tested pure core: `quantmind.portfolio`
for the Portfolio/Position types, `quantmind.core.snapshot` for the identity
every risk result keys off, `quantmind.exposure.book_greeks` for per-underlier
Greeks/dollar-delta/stress grid (A3's engine, reused not re-derived), and
`quantmind.exposure.attribution` for the core-vs-overlay P&L split (Task B1's
new pure module).

Serialization policy (repo-wide, api/app.py): UTC ISO-Z timestamps, NaN/Inf ->
null, missing/empty book -> structured empty, never a 500. Prices come from
the cached bar store only (no network call here) — a position with no cached
bars degrades to null price/market_value/weight, it never crashes the route.

Book source (Task A1's book-flow spine): `book_ref` (optional query param) —
absent, this is the LIVE broker book exactly as before (back-compatible: the
original five response fields are unchanged in shape); given, the book is
resolved from a pinned snapshot via routers/book.py's `read_book_positions`,
which is the ONLY path an OPT leg's strike/expiry/right ever reaches this
router — `Position` (Engineering Constraint 9's one Portfolio type) has no
room for those fields, so a live-broker OPT position can never be priced for
Greeks/expiry-buckets no matter how it's fetched (see routers/book.py's
`read_book_positions` docstring for the same limit on the whatif/hedge/options
side). That is the source of the options sleeve's two honest-empty reasons:
"no option positions" (none held) vs "chain not ingested — run options_sync"
(held, but this router has no way to price them — either no book_ref was
given, or the pinned book's legs lack a matching cached IV quote).

New response fields are purely ADDITIVE (schema choice, task note): every
field present before this task keeps its exact shape, so existing consumers
of GET /api/portfolio (there are none yet outside web/src/pages/Portfolio.tsx,
owned by this same task) are unaffected; no v2 route was needed.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import clean, downsample, iso, weighted_portfolio_returns
from quantmind.api.routers.book import read_book_positions
from quantmind.core.snapshot import BookSnapshot
from quantmind.datastore.options_store import OptionsStore
from quantmind.exposure.attribution import InsufficientDataError as AttributionInsufficientDataError
from quantmind.exposure.attribution import decompose_book_pnl, summarize_pnl_split
from quantmind.exposure.book_greeks import BookLeg, aggregate_book_stress_grid, compute_book_greeks
from quantmind.portfolio import Portfolio, Position
from quantmind.risk.returns import InsufficientDataError as ReturnsInsufficientDataError
from quantmind.risk.returns import rolling_beta

router = APIRouter()

# Rolling-beta window (per-underlier exposure beta AND the book-level beta
# attribution uses) — matches routers/risk.py's/routers/hedge.py's own
# window+rf=0 convention exactly, so a beta shown here is comparable to one
# shown on the Risk page.
_BETA_WINDOW = 60
_RISK_FREE_RATE = 0.0
_YEARS_OF_HISTORY = 5
_MAX_ATTRIBUTION_POINTS = 500
_EXPIRY_BUCKET_DAYS = (7, 30, 90)

# Never-500 law, news.py's request-failed-note pattern for the same
# Gateway-death condition (batch-1 final review F3): the flagship page
# degrades to an honest empty book + note when the broker dies mid-session.
_BROKER_FAILED_NOTE = "book request failed — Gateway connection error; showing empty book"


class PositionOut(BaseModel):
    con_id: int
    symbol: str
    qty: float
    sec_type: str
    multiplier: float
    last_close: float | None
    market_value: float | None
    weight: float | None
    # Ledger essentials (Task B1): cost basis + unrealized P&L. Both null
    # when the broker doesn't report avgCost for this con_id (no broker, a
    # book_ref-resolved hypothetical book, or a live broker that never sent
    # this position's cost basis) — mark is the cached last close, honest
    # null when either side of the P&L calc is missing.
    avg_cost: float | None = None
    unrealized_pnl: float | None = None


class Totals(BaseModel):
    market_value: float | None
    n_positions: int
    unrealized_pnl: float | None = None


class AccountOut(BaseModel):
    net_liquidation: float | None
    total_cash_value: float | None
    gross_position_value: float | None
    buying_power: float | None


class UnderlyingExposureOut(BaseModel):
    """Delta-adjusted exposure (Task B1 requirement 2 — "the number he
    manages to"): net delta (shares + option legs via book_greeks),
    dollar-delta, and SPY-equivalent notional (dollar-delta * per-underlier
    beta vs the app benchmark, estimated from cached bars)."""

    underlier: str
    spot: float | None
    net_delta: float | None
    dollar_delta: float | None
    beta: float | None
    spy_equivalent_notional: float | None
    beta_note: str | None


class SleeveUnderlyingOut(BaseModel):
    underlier: str
    gamma: float | None
    vega: float | None
    theta: float | None


class StressGridOut(BaseModel):
    vol_shocks: list[float]
    spot_shocks: list[float]
    pnl: list[list[float | None]]  # rows = vol_shocks, cols = spot_shocks


class OptionsSleeveOut(BaseModel):
    """Requirement 3: per-underlying net Gamma/vega/theta + the spot x vol
    stress grid — renders only when option positions AND priceable chain
    data exist; the two honest-empty `reason`s are documented on the module
    docstring above."""

    available: bool
    reason: str | None
    underlyings: list[SleeveUnderlyingOut]
    stress_grid: StressGridOut | None


class ExpiryLegOut(BaseModel):
    symbol: str
    expiry: str
    right: str
    strike: float
    qty: float
    days_to_expiry: int


class ExpiryBucketsOut(BaseModel):
    """Requirement 5: option legs bucketed by days-to-expiry. Only legs that
    resolved to a priceable BookLeg (matching cached-chain IV) are bucketed —
    an unpriceable OPT position has no known expiry to bucket by."""

    le_7d: list[ExpiryLegOut]
    le_30d: list[ExpiryLegOut]
    le_90d: list[ExpiryLegOut]
    later: list[ExpiryLegOut]


class AttributionPointOut(BaseModel):
    date: str
    total_pnl: float | None
    core_pnl: float | None
    overlay_pnl: float | None


class AttributionOut(BaseModel):
    """Requirement 4 — the product's identity number: daily book P&L
    decomposed into beta*bench_return*book_value (core) vs the residual
    (overlay), over `window_days` of the cached history. Pure math lives in
    exposure/attribution.py; this router only assembles the return series and
    estimates beta (quantmind.risk.returns.rolling_beta, the same estimator
    routers/risk.py and routers/hedge.py use)."""

    available: bool
    reason: str | None
    window_days: int
    beta: float | None
    n_obs: int
    total_pnl: float | None
    core_pnl: float | None
    overlay_pnl: float | None
    core_share: float | None
    overlay_share: float | None
    series: list[AttributionPointOut]


class PortfolioResponse(BaseModel):
    snapshot_id: str
    valuation_ts: str
    base_currency: str
    positions: list[PositionOut]
    totals: Totals
    account: AccountOut | None
    # DESIGN.md convention: "NO MATERIAL LINK is stated honestly when
    # portfolio linkage is immaterial" — explains a null `account` (no
    # broker, a book_ref-resolved hypothetical book, or a broker that
    # doesn't report account summary) rather than leaving it unexplained.
    account_note: str | None
    exposure: list[UnderlyingExposureOut]
    options_sleeve: OptionsSleeveOut
    expiry_buckets: ExpiryBucketsOut
    attribution: AttributionOut


def _close_series(store, con_id: int) -> pd.Series | None:
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        return None
    if bars.empty:
        return None
    return bars["close"]


def _last_close(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    return clean(float(series.iloc[-1]))


def _last_beta(asset: pd.Series, bench: pd.Series) -> float | None:
    aligned = pd.concat({"asset": asset, "bench": bench}, axis=1).dropna()
    if len(aligned) < _BETA_WINDOW + 2:
        return None
    try:
        beta_series = rolling_beta(aligned["asset"], aligned["bench"], window=_BETA_WINDOW, rf=_RISK_FREE_RATE)
    except ReturnsInsufficientDataError:
        return None
    beta_valid = beta_series.dropna()
    if beta_valid.empty:
        return None
    val = float(beta_valid.iloc[-1])
    return val if math.isfinite(val) else None


async def _resolve_avg_costs(broker) -> dict[int, float]:
    """Optional cost-basis enrichment (Task B1): a broker that doesn't
    implement `get_avg_costs` (or fails to report it) degrades to an honest
    empty mapping — never a 500. Intentionally broad except: this is a
    side-channel enrichment on top of the already-fetched book, not the
    book itself, so any failure here must never break the page."""
    get_avg_costs = getattr(broker, "get_avg_costs", None)
    if get_avg_costs is None:
        return {}
    try:
        return await get_avg_costs()
    except Exception:
        return {}


async def _resolve_account(broker, book_ref: str | None) -> tuple[AccountOut | None, str | None]:
    if book_ref is not None:
        return None, "NO MATERIAL LINK — account values reflect the live broker connection, not this pinned book"
    if broker is None:
        return None, "NO MATERIAL LINK — no broker connected"
    get_account_summary = getattr(broker, "get_account_summary", None)
    if get_account_summary is None:
        return None, "broker does not report account summary"
    try:
        summary = await get_account_summary()
        # Construction lives INSIDE the try (F6): a partial dict raises
        # ValidationError -> honest null account, never a 500; clean() keeps
        # a NaN/Inf float from ever reaching the serialized response.
        account = AccountOut(**{k: clean(v) for k, v in summary.items()})
    except Exception:
        return None, "account summary unavailable (broker request failed or malformed summary)"
    return account, None


def _book_from_book_ref(store, book_ref: str) -> tuple[Portfolio, list[object | None]]:
    """Resolve a pinned snapshot into a priced Portfolio + the option-leg
    fields (strike/expiry/right), returned as a list POSITIONALLY ALIGNED
    with `portfolio.positions` (index i's leg belongs to position i) — never
    keyed by con_id. book.py's `_portfolio_from_positions` (the same
    construction whatif/hedge/book/options all key off) assigns
    `con_id=symbol_map[symbol]` uniformly, so every option leg on the same
    underlier shares ONE con_id regardless of strike/expiry — a con_id-keyed
    map would silently collapse a multi-leg book (e.g. a call spread) down to
    a single leg. Positional pairing is exactly the convention book.py's own
    `write_book`/`read_book_positions` already use for this reason."""
    legs = read_book_positions(store, book_ref)
    symbol_map = store.read_symbol_map()
    unknown = sorted({leg.symbol for leg in legs} - symbol_map.keys())
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")

    positions = []
    option_legs: list[object | None] = []
    for leg in legs:
        multiplier = leg.multiplier if leg.multiplier is not None else (100.0 if leg.right is not None else 1.0)
        positions.append(
            Position(
                con_id=symbol_map[leg.symbol],
                symbol=leg.symbol,
                qty=leg.qty,
                sec_type="OPT" if leg.right is not None else "STK",
                multiplier=multiplier,
            )
        )
        option_legs.append(leg if leg.right is not None else None)
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Portfolio(positions=tuple(positions), as_of=valuation_ts), option_legs


def _find_quote_row(df: pd.DataFrame, expiry: str, strike: float, right: str) -> pd.Series | None:
    mask = (
        (df["expiry"].astype(str) == expiry)
        & (df["right"].astype(str) == right)
        & np.isclose(df["strike"].astype(float), strike, atol=1e-6)
    )
    matched = df.loc[mask]
    return matched.iloc[0] if not matched.empty else None


def _priced_option_leg(
    p: Position,
    leg,
    options_store: OptionsStore,
    spot: float,
    as_of: date,
) -> tuple[BookLeg, ExpiryLegOut] | None:
    """A single OPT position's book leg + expiry-bucket row, IF its strike/
    expiry has a matching cached-chain quote with a usable IV — else None
    (the caller folds that into the sleeve's honest-empty reason)."""
    if leg is None or leg.strike is None or leg.expiry is None or leg.right is None:
        return None
    if not options_store.has_chain(p.symbol):
        return None
    try:
        chain_df, _ = options_store.read_chain(p.symbol)
    except FileNotFoundError:
        return None
    row = _find_quote_row(chain_df, expiry=leg.expiry, strike=leg.strike, right=leg.right)
    if row is None:
        return None
    iv = row.iv
    if iv is None or not np.isfinite(float(iv)):
        return None
    try:
        expiry_dt = datetime.strptime(leg.expiry, "%Y%m%d").date()
    except ValueError:
        return None
    days_to_expiry = (expiry_dt - as_of).days
    expiry_years = days_to_expiry / 365.25
    if expiry_years <= 0:
        return None
    multiplier = leg.multiplier if leg.multiplier is not None else float(row.multiplier)

    book_leg = BookLeg(
        underlier=p.symbol,
        qty=p.qty,
        is_option=True,
        spot=spot,
        r=_RISK_FREE_RATE,
        strike=leg.strike,
        expiry_years=expiry_years,
        is_call=(leg.right == "C"),
        iv=float(iv),
        multiplier=multiplier,
    )
    bucket_row = ExpiryLegOut(
        symbol=p.symbol, expiry=leg.expiry, right=leg.right, strike=leg.strike, qty=p.qty,
        days_to_expiry=days_to_expiry,
    )
    return book_leg, bucket_row


def _bucket_expiry_legs(rows: list[ExpiryLegOut]) -> ExpiryBucketsOut:
    le7, le30, le90, later = [], [], [], []
    for row in rows:
        if row.days_to_expiry <= _EXPIRY_BUCKET_DAYS[0]:
            le7.append(row)
        elif row.days_to_expiry <= _EXPIRY_BUCKET_DAYS[1]:
            le30.append(row)
        elif row.days_to_expiry <= _EXPIRY_BUCKET_DAYS[2]:
            le90.append(row)
        else:
            later.append(row)
    return ExpiryBucketsOut(le_7d=le7, le_30d=le30, le_90d=le90, later=later)


def _book_return_series(
    close_series: dict[int, pd.Series], market_values: dict[int, float]
) -> tuple[pd.Series, float] | None:
    """Weighted daily return series over every priced conId (weights are
    fractions of GROSS |market value| — hedge.py's `_portfolio_returns`
    convention), plus the gross value the caller must use as the dollar
    scale for any per-book-dollar quantity built from this series (module
    docstring's normalization convention, same one routers/hedge.py's
    overlay math documents). `market_values` is per-CONID (the caller sums
    per-position values into it — multiple legs sharing a conId share one
    return column, so their dollar weights add)."""
    con_ids = [cid for cid, series in close_series.items() if market_values.get(cid) is not None]
    if not con_ids:
        return None
    gross = sum(abs(market_values[cid]) for cid in con_ids)
    if not gross:
        return None
    prices = pd.concat({cid: close_series[cid] for cid in con_ids}, axis=1).dropna()
    if len(prices) < 2:
        return None
    returns = prices.pct_change().dropna()
    if returns.empty:
        return None
    weights = np.array([market_values[cid] / gross for cid in con_ids])
    return weighted_portfolio_returns(returns, con_ids, weights), gross


def _compute_attribution(
    store,
    close_series: dict[int, pd.Series],
    market_values: dict[int, float],
    benchmark: str,
    symbol_map: dict[str, int],
    window_days: int,
) -> AttributionOut:
    empty = lambda reason: AttributionOut(  # noqa: E731
        available=False, reason=reason, window_days=window_days, beta=None, n_obs=0,
        total_pnl=None, core_pnl=None, overlay_pnl=None, core_share=None, overlay_share=None, series=[],
    )

    result = _book_return_series(close_series, market_values)
    if result is None:
        return empty("no priced positions with enough overlapping history for a book return series")
    book_returns, gross = result

    if benchmark not in symbol_map:
        return empty(f"benchmark {benchmark!r} not in cache")
    bench_series = _close_series(store, symbol_map[benchmark])
    if bench_series is None:
        return empty(f"benchmark {benchmark!r} has no cached bars")
    bench_returns = bench_series.pct_change().dropna()

    aligned = pd.concat({"book": book_returns, "bench": bench_returns}, axis=1).dropna()
    if len(aligned) < _BETA_WINDOW + 2:
        return empty(
            f"only {len(aligned)} overlapping book/benchmark observations; need > window+1 ({_BETA_WINDOW + 1})"
        )

    try:
        beta_series = rolling_beta(aligned["book"], aligned["bench"], window=_BETA_WINDOW, rf=_RISK_FREE_RATE)
    except ReturnsInsufficientDataError:
        return empty("insufficient data to estimate book beta")
    beta_valid = beta_series.dropna()
    if beta_valid.empty:
        return empty("insufficient data to estimate book beta")
    beta = float(beta_valid.iloc[-1])
    if not math.isfinite(beta):
        return empty("book beta estimate is non-finite")

    windowed_book = aligned["book"].iloc[-window_days:]
    windowed_bench = aligned["bench"].iloc[-window_days:]
    try:
        decomposed = decompose_book_pnl(windowed_book, windowed_bench, beta=beta, book_value=gross)
    except AttributionInsufficientDataError:
        return empty("no overlapping observations in the requested attribution window")

    summary = summarize_pnl_split(decomposed)
    points = [
        AttributionPointOut(
            date=iso(idx), total_pnl=clean(row.total_pnl), core_pnl=clean(row.core_pnl),
            overlay_pnl=clean(row.overlay_pnl),
        )
        for idx, row in decomposed.iterrows()
    ]
    points = downsample(points, _MAX_ATTRIBUTION_POINTS)

    return AttributionOut(
        available=True,
        reason=None,
        window_days=window_days,
        beta=clean(beta),
        n_obs=summary.n_obs,
        total_pnl=clean(summary.total_pnl),
        core_pnl=clean(summary.core_pnl),
        overlay_pnl=clean(summary.overlay_pnl),
        core_share=clean(summary.core_share),
        overlay_share=clean(summary.overlay_share),
        series=points,
    )


@router.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio(
    request: Request,
    book_ref: str | None = Query(None),
    attribution_days: int = Query(90, ge=5, le=756),
) -> PortfolioResponse:
    store = request.app.state.store
    broker = request.app.state.broker
    benchmark = request.app.state.benchmark

    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    broker_failed = False
    if book_ref is not None:
        portfolio, option_legs = _book_from_book_ref(store, book_ref)
        valuation_ts = portfolio.as_of
    elif broker is None:
        portfolio = Portfolio(positions=(), as_of=valuation_ts)
        option_legs = []
    else:
        try:
            portfolio = await broker.get_portfolio()
        except Exception:
            # Gateway death mid-session (F3): degrade exactly like
            # broker=None, but say so honestly via _BROKER_FAILED_NOTE.
            portfolio = Portfolio(positions=(), as_of=valuation_ts)
            broker_failed = True
        # Live-broker positions never carry strike/expiry (module docstring)
        # — one `None` leg per position, positionally aligned like the
        # book_ref path.
        option_legs = [None] * len(portfolio.positions)

    snapshot = BookSnapshot.create(portfolio, valuation_ts=valuation_ts, base_currency="USD")

    # --- ledger essentials: price, cost basis, unrealized P&L ---
    avg_costs: dict[int, float] = {}
    if book_ref is None and broker is not None and not broker_failed:
        avg_costs = await _resolve_avg_costs(broker)

    # Per-POSITION lists, positionally aligned with portfolio.positions —
    # never keyed by con_id (fix-round-1 CRITICAL, reviewer live-reproduced):
    # a book_ref book's legs all share the synthetic con_id
    # (= symbol_map[underlier], the same collapse already fixed for
    # exposure/expiry_buckets), so a con_id-keyed dict here made every leg
    # of a multi-leg book overwrite the last — a 2-leg SPY book reported
    # both positions at the LAST leg's market value and weight 1.0 each.
    # `close_series` alone stays con_id-keyed: the price SERIES really is
    # per-conId (every leg on one underlier reads the same bars).
    close_series: dict[int, pd.Series] = {}
    last_closes: list[float | None] = []
    market_values: list[float | None] = []
    for p in portfolio.positions:
        if p.con_id not in close_series:
            series = _close_series(store, p.con_id)
            if series is not None:
                close_series[p.con_id] = series
        last_close = _last_close(close_series.get(p.con_id))
        last_closes.append(last_close)
        market_values.append(clean(p.qty * p.multiplier * last_close) if last_close is not None else None)

    known_mvs = [mv for mv in market_values if mv is not None]
    total_mv = sum(known_mvs) if known_mvs else None

    positions_out: list[PositionOut] = []
    unrealized_values: list[float] = []
    for p, last_close, mv in zip(portfolio.positions, last_closes, market_values):
        avg_cost = clean(avg_costs.get(p.con_id))
        unrealized = (
            clean((last_close - avg_cost) * p.qty * p.multiplier)
            if last_close is not None and avg_cost is not None
            else None
        )
        if unrealized is not None:
            unrealized_values.append(unrealized)
        positions_out.append(
            PositionOut(
                con_id=p.con_id,
                symbol=p.symbol,
                qty=p.qty,
                sec_type=p.sec_type,
                multiplier=p.multiplier,
                last_close=last_close,
                market_value=mv,
                weight=(mv / total_mv if mv is not None and total_mv else None),
                avg_cost=avg_cost,
                unrealized_pnl=unrealized,
            )
        )

    totals = Totals(
        market_value=total_mv,
        n_positions=len(portfolio.positions),
        unrealized_pnl=(sum(unrealized_values) if unrealized_values else None),
    )

    if broker_failed:
        account, account_note = None, _BROKER_FAILED_NOTE
    else:
        account, account_note = await _resolve_account(broker, book_ref)

    # --- delta-adjusted exposure + options sleeve + expiry buckets ---
    symbol_map = store.read_symbol_map()
    options_store = OptionsStore(store.root)
    today = date.today()

    groups: dict[str, list[tuple[Position, object | None]]] = {}
    for p, leg in zip(portfolio.positions, option_legs):
        groups.setdefault(p.symbol, []).append((p, leg))

    has_option_positions = any(p.sec_type == "OPT" for p in portfolio.positions)
    book_legs: list[BookLeg] = []
    priced_option_underliers: set[str] = set()
    # Option underliers dropped BEFORE the chain lookup (no symbol-map entry
    # or no cached bars) — the sleeve's honest-empty reason must name them
    # rather than blame the chain (fix-round-1 minor: an unknown-symbol OPT
    # leg used to fall through to the generic "chain not ingested" reason).
    unpriceable_option_underliers: set[str] = set()
    expiry_rows: list[ExpiryLegOut] = []
    underlier_betas: dict[str, float | None] = {}

    for underlier, group in groups.items():
        group_has_options = any(p.sec_type == "OPT" for p, _ in group)
        if underlier not in symbol_map:
            if group_has_options:
                unpriceable_option_underliers.add(underlier)
            continue
        spot_series = _close_series(store, symbol_map[underlier])
        spot = _last_close(spot_series)
        if spot is None:
            if group_has_options:
                unpriceable_option_underliers.add(underlier)
            continue

        if underlier == benchmark:
            beta: float | None = 1.0
        else:
            bench_series = _close_series(store, symbol_map[benchmark]) if benchmark in symbol_map else None
            beta = _last_beta(spot_series, bench_series) if bench_series is not None else None
        underlier_betas[underlier] = beta

        for p, leg in group:
            if p.sec_type != "OPT":
                book_legs.append(BookLeg(underlier=underlier, qty=p.qty, is_option=False, spot=spot, r=_RISK_FREE_RATE))
                continue
            resolved = _priced_option_leg(p, leg, options_store, spot, today)
            if resolved is None:
                continue
            book_leg, bucket_row = resolved
            book_legs.append(book_leg)
            expiry_rows.append(bucket_row)
            priced_option_underliers.add(underlier)

    betas_clean = {k: v for k, v in underlier_betas.items() if v is not None}
    underlyings = compute_book_greeks(book_legs, betas=betas_clean) if book_legs else []

    exposure_out = [
        UnderlyingExposureOut(
            underlier=u.underlier,
            spot=clean(u.spot),
            net_delta=clean(u.delta),
            dollar_delta=clean(u.dollar_delta),
            beta=clean(underlier_betas.get(u.underlier)),
            spy_equivalent_notional=clean(u.spy_equivalent_notional),
            beta_note=(
                None
                if underlier_betas.get(u.underlier) is not None
                else f"insufficient history for beta vs {benchmark} ({_BETA_WINDOW}d window)"
            ),
        )
        for u in underlyings
    ]

    if not has_option_positions:
        options_sleeve = OptionsSleeveOut(available=False, reason="no option positions", underlyings=[], stress_grid=None)
    elif not priced_option_underliers:
        if unpriceable_option_underliers:
            reason = (
                f"option underliers not in cached universe: {sorted(unpriceable_option_underliers)} — "
                "sync bars first"
            )
        else:
            reason = "chain not ingested — run options_sync"
        options_sleeve = OptionsSleeveOut(available=False, reason=reason, underlyings=[], stress_grid=None)
    else:
        sleeve_legs = [leg for leg in book_legs if leg.underlier in priced_option_underliers]
        grid = aggregate_book_stress_grid(sleeve_legs)
        options_sleeve = OptionsSleeveOut(
            available=True,
            reason=None,
            underlyings=[
                SleeveUnderlyingOut(underlier=u.underlier, gamma=clean(u.gamma), vega=clean(u.vega), theta=clean(u.theta))
                for u in underlyings
                if u.underlier in priced_option_underliers
            ],
            stress_grid=StressGridOut(
                vol_shocks=[float(v) for v in grid.index],
                spot_shocks=[float(c) for c in grid.columns],
                pnl=[[clean(v) for v in row] for row in grid.to_numpy().tolist()],
            ),
        )

    expiry_buckets = _bucket_expiry_legs(expiry_rows)

    # Attribution's book-return series IS per-conId (one price series per
    # conId), so per-position market values are SUMMED into a per-conId
    # dollar weight — legs sharing a conId share a return column, and their
    # dollar exposures simply add (the same weights-add convention
    # _shared.weighted_portfolio_returns documents for repeated symbols).
    mv_by_conid: dict[int, float] = {}
    for p, mv in zip(portfolio.positions, market_values):
        if mv is not None:
            mv_by_conid[p.con_id] = mv_by_conid.get(p.con_id, 0.0) + mv

    attribution = _compute_attribution(
        store, close_series, mv_by_conid, benchmark, symbol_map, attribution_days
    )

    return PortfolioResponse(
        snapshot_id=snapshot.snapshot_id,
        valuation_ts=valuation_ts,
        base_currency="USD",
        positions=positions_out,
        totals=totals,
        account=account,
        account_note=account_note,
        exposure=exposure_out,
        options_sleeve=options_sleeve,
        expiry_buckets=expiry_buckets,
        attribution=attribution,
    )
