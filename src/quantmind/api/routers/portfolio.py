"""Portfolio domain routes — the truth about the book (DESIGN.md: "the truth
about the book"). Thin wrapper over the tested pure core: `quantmind.portfolio`
for the Portfolio/Position types, `quantmind.core.snapshot` for the identity
every risk result keys off, `quantmind.exposure.book_greeks` for per-underlier
Greeks/dollar-delta/stress grid (A3's engine, reused not re-derived), and
`quantmind.exposure.attribution` for the core-vs-overlay P&L split (Task B1's
new pure module).

Serialization policy (repo-wide, api/app.py): UTC ISO-Z timestamps, NaN/Inf ->
null, missing/empty book -> structured empty, never a 500. Equity marks come
from cached bars; option marks come only from their exact cached-chain row.
Neither path makes a network call, and an unpriceable position degrades to
null price/market_value/weight rather than borrowing its underlier's price.

Book source (Task A1's book-flow spine): `book_ref` (optional query param) —
absent, this is the LIVE broker book exactly as before (back-compatible: the
original five response fields are unchanged in shape); given, the book is
resolved from a pinned snapshot via routers/book.py's `read_book_positions`,
which is also how hypothetical option terms reach this router. Live IBKR
positions preserve strike/expiry/right on `Position`, allowing the same
options sleeve to price a live book when a matching chain is cached. That is
the source of the options sleeve's two honest-empty reasons:
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
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import (
    FxEvidenceOut,
    PositionIn,
    clean,
    complete_fx_evidence,
    downsample,
    iso,
    latest_observation_is_future,
    mapped_instrument_metadata,
    read_instrument_metadata_map,
    weighted_portfolio_returns,
)
from quantmind.api.routers.book import (
    ResolvedBookPosition,
    read_book,
    read_book_positions,
    validate_live_stock_identities,
    validate_pinned_book_scope,
    validate_pinned_instrument_identities,
)
from quantmind.core.snapshot import BookSnapshot
from quantmind.datastore.options_store import (
    OptionsSnapshotMeta,
    OptionsStore,
    option_chain_freshness,
)
from quantmind.exposure.attribution import InsufficientDataError as AttributionInsufficientDataError
from quantmind.exposure.attribution import decompose_book_pnl, summarize_pnl_split
from quantmind.exposure.book_greeks import BookLeg, aggregate_book_stress_grid, compute_book_greeks
from quantmind.fx import FxConversionUnavailable, FxConverter
from quantmind.portfolio import Portfolio, Position, option_terms_complete
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
_MARK_STALE_AFTER_DAYS = 3
_OPTIONS_STALE_AFTER_DAYS = 3


class PositionOut(BaseModel):
    con_id: int
    symbol: str
    qty: float
    sec_type: str
    multiplier: float
    # Exact broker/canonical identity is part of the reconciliation surface,
    # not merely an implementation detail.  Keep option terms on every row
    # even when the chain is missing and valuation therefore degrades to null.
    exchange: str | None
    strike: float | None
    expiry: str | None
    right: Literal["C", "P"] | None
    currency: str | None
    last_close: float | None
    mark_as_of: str | None
    fx_rate_to_base: float | None
    local_market_value: float | None
    market_value: float | None
    weight: float | None
    # Ledger essentials (Task B1): cost basis + unrealized P&L. Both null
    # when the broker doesn't report avgCost for this con_id (no broker, a
    # book_ref-resolved hypothetical book, or a live broker that never sent
    # this position's cost basis) — mark is the cached last close, honest
    # null when either side of the P&L calc is missing.
    avg_cost: float | None = None
    unrealized_pnl_local: float | None = None
    unrealized_pnl: float | None = None


class Totals(BaseModel):
    market_value: float | None
    priced_market_value: float | None
    n_positions: int
    priced_positions: int
    valuation_status: Literal["empty", "partial", "complete"]
    unrealized_pnl: float | None = None
    reported_unrealized_pnl: float | None = None
    pnl_status: Literal["empty", "partial", "complete"]


class AccountOut(BaseModel):
    net_liquidation: float | None
    total_cash_value: float | None
    gross_position_value: float | None
    buying_power: float | None
    net_liquidation_base: float | None
    total_cash_value_base: float | None
    gross_position_value_base: float | None
    buying_power_base: float | None
    currency: str
    source_currency: str


class UnderlyingExposureOut(BaseModel):
    """Delta-adjusted exposure (Task B1 requirement 2 — "the number he
    manages to"): net delta (shares + option legs via book_greeks),
    dollar-delta, and SPY-equivalent notional (dollar-delta * per-underlier
    beta vs the app benchmark, estimated from cached bars)."""

    underlier: str
    currency: str | None = None
    fx_rate_to_base: float | None = None
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
    data exist. Completeness counts legs with an exact usable mark, IV, and
    underlier spot — every input required by the sleeve analytics."""

    available: bool
    status: Literal["complete", "partial", "unavailable"]
    total_positions: int
    priced_positions: int
    missing_positions: int
    chain_as_of: str | None
    chain_age_days: int | None
    chain_stale: bool | None
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
    # ``valuation_ts`` is the immutable book/snapshot timestamp. Marks are
    # refreshed independently, so their weakest observation date is exposed
    # separately instead of relabeling a pin time as a market-data as-of.
    valuation_ts: str
    market_data_as_of: str | None
    base_currency: str
    fx: FxEvidenceOut
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
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None
    if bars.empty:
        return None
    return bars["close"]


def _last_close(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    return clean(float(series.iloc[-1]))


def _last_observation_date(series: pd.Series | None) -> date | None:
    """Return the date belonging to the accepted last market observation.

    Spot conversion must be keyed to the mark itself, not the request date:
    otherwise a newer FX observation can leak into an older cached close.
    """
    if series is None or series.empty:
        return None
    try:
        return pd.Timestamp(series.index[-1]).date()
    except (TypeError, ValueError):
        return None


def _series_is_stale(series: pd.Series | None, today: date) -> bool:
    if series is None or series.empty:
        return True
    try:
        if latest_observation_is_future(series, today=today):
            return True
        observation = pd.Timestamp(series.index[-1]).date()
    except (TypeError, ValueError):
        return True
    age_days = int(np.busday_count(observation.isoformat(), today.isoformat()))
    return age_days > _MARK_STALE_AFTER_DAYS


def _last_beta(asset: pd.Series, bench: pd.Series) -> float | None:
    aligned = pd.concat({"asset": asset, "bench": bench}, axis=1).dropna()
    if len(aligned) < _BETA_WINDOW + 2:
        return None
    returns = aligned.pct_change().dropna()
    try:
        beta_series = rolling_beta(
            returns["asset"],
            returns["bench"],
            window=_BETA_WINDOW,
            rf=_RISK_FREE_RATE,
        )
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


async def _resolve_account(
    broker,
    book_ref: str | None,
    *,
    store,
    base_currency: str,
    as_of: date,
) -> tuple[AccountOut | None, str | None, FxEvidenceOut | None]:
    if book_ref is not None:
        return None, "NO MATERIAL LINK — account values reflect the live broker connection, not this pinned book", None
    if broker is None:
        return None, "NO MATERIAL LINK — no broker connected", None
    get_account_summary = getattr(broker, "get_account_summary", None)
    if get_account_summary is None:
        return None, "broker does not report account summary", None
    try:
        summary = await get_account_summary()
    except Exception:
        return None, "account summary unavailable (broker request failed)", None
    currency = summary.get("currency")
    if currency is None:
        return (
            None,
            "account summary withheld because the broker did not label its base currency",
            None,
        )
    source_currency = str(currency).strip().upper()
    monetary_fields = (
        "net_liquidation",
        "total_cash_value",
        "gross_position_value",
        "buying_power",
    )
    local = {field: clean(summary.get(field)) for field in monetary_fields}
    rate = 1.0
    if source_currency != base_currency:
        try:
            converter = FxConverter.from_store(
                store,
                base_currency=base_currency,
                currencies={source_currency},
            )
            rate = converter.rate(source_currency, as_of)
        except (FxConversionUnavailable, ValueError) as exc:
            account = AccountOut(
                **local,
                **{f"{field}_base": None for field in monetary_fields},
                currency=source_currency,
                source_currency=source_currency,
            )
            return (
                account,
                f"Broker totals remain in {source_currency}; dated normalization to "
                f"{base_currency} is unavailable ({exc}). Run sync to refresh FX.",
                FxEvidenceOut(
                    status="incomplete",
                    base_currency=base_currency,
                    source=None,
                    as_of=None,
                    fetched_at=None,
                    missing_currencies=[source_currency],
                    note=(
                        f"Broker account values in {source_currency} could not be "
                        f"normalized to {base_currency}."
                    ),
                ),
            )
    converted = {
        f"{field}_base": (
            clean(summary.get(field) * rate)
            if summary.get(field) is not None
            else None
        )
        for field in monetary_fields
    }
    account = AccountOut(
        **local,
        **converted,
        currency=source_currency,
        source_currency=source_currency,
    )
    account_evidence = (
        complete_fx_evidence(converter, base_currency=base_currency)
        if source_currency != base_currency
        else FxEvidenceOut(
            status="identity",
            base_currency=base_currency,
            source=None,
            as_of=None,
            fetched_at=None,
            missing_currencies=[],
            note=f"Broker account values are denominated in {base_currency}.",
        )
    )
    return account, None, account_evidence


def _merge_fx_evidence(
    position_evidence: FxEvidenceOut,
    account_evidence: FxEvidenceOut | None,
    *,
    base_currency: str,
) -> FxEvidenceOut:
    """Combine position and live-account conversion provenance without hiding either."""
    if account_evidence is None:
        return position_evidence
    missing = sorted(
        set(position_evidence.missing_currencies) | set(account_evidence.missing_currencies)
    )
    sources = sorted(
        {
            source
            for source in (position_evidence.source, account_evidence.source)
            if source
        }
    )
    as_of_values = [
        value for value in (position_evidence.as_of, account_evidence.as_of) if value
    ]
    fetched_values = [
        value
        for value in (position_evidence.fetched_at, account_evidence.fetched_at)
        if value
    ]
    converted = bool(sources)
    return FxEvidenceOut(
        status="incomplete" if missing else "converted" if converted else "identity",
        base_currency=base_currency,
        source=", ".join(sources) or None,
        as_of=min(as_of_values) if as_of_values else None,
        fetched_at=min(fetched_values) if fetched_values else None,
        missing_currencies=missing,
        note=(
            f"Account or position values in {missing} could not be normalized to "
            f"{base_currency}; available local values are retained."
            if missing
            else f"Portfolio positions and broker account values are normalized to "
            f"{base_currency} with dated FX evidence."
            if converted
            else f"Portfolio positions and broker account values are denominated in "
            f"{base_currency}."
        ),
    )


def _book_from_book_ref(
    store,
    book_ref: str,
    *,
    valuation_ts: str,
    use_persisted_contract_ids: bool,
) -> tuple[Portfolio, list[object | None]]:
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
        resolved_leg = (
            leg
            if use_persisted_contract_ids
            else leg.model_copy(update={"con_id": None})
        )
        multiplier = leg.multiplier if leg.multiplier is not None else (100.0 if leg.right is not None else 1.0)
        positions.append(
            Position(
                con_id=(
                    resolved_leg.con_id
                    if resolved_leg.right is not None and resolved_leg.con_id is not None
                    else symbol_map[leg.symbol]
                ),
                symbol=leg.symbol,
                qty=leg.qty,
                sec_type=leg.sec_type,
                multiplier=multiplier,
                strike=leg.strike,
                expiry=leg.expiry,
                right=leg.right,
                currency=leg.currency,
                exchange=leg.exchange,
            )
        )
        option_legs.append(resolved_leg if resolved_leg.right is not None else None)
    return Portfolio(positions=tuple(positions), as_of=valuation_ts), option_legs


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


def _quote_mark(row: pd.Series) -> float | None:
    """Midpoint when two-sided; otherwise the sole usable bid or ask.

    The cached-chain schema has no last-trade field, so there is deliberately
    no invented last-price fallback here.
    """
    bid = clean(getattr(row, "bid", None))
    ask = clean(getattr(row, "ask", None))
    bid = bid if bid is not None and bid >= 0 else None
    ask = ask if ask is not None and ask >= 0 else None
    if bid is not None and ask is not None:
        if ask < bid:
            return None
        return (bid + ask) / 2
    return bid if bid is not None else ask


def _cached_option_chain(
    underlier: str,
    expected_underlier_con_id: int | None,
    options_store: OptionsStore,
    cache: dict[str, tuple[pd.DataFrame, OptionsSnapshotMeta] | None],
) -> tuple[pd.DataFrame, OptionsSnapshotMeta] | None:
    """Read each underlier's parquet snapshot at most once per request."""
    if underlier not in cache:
        try:
            cached = options_store.read_chain(underlier)
            cache[underlier] = (
                cached
                if expected_underlier_con_id is not None
                and cached[1].underlier_con_id == expected_underlier_con_id
                else None
            )
        except (FileNotFoundError, KeyError, OSError, ValueError):
            cache[underlier] = None
    return cache[underlier]


def _option_mark(
    p: Position,
    leg,
    options_store: OptionsStore,
    chain_cache: dict[str, tuple[pd.DataFrame, OptionsSnapshotMeta] | None],
    today: date,
    expected_underlier_con_id: int | None,
) -> tuple[float | None, date | None]:
    """Return an exact contract's cached midpoint or usable one-sided quote."""
    if leg is None or leg.strike is None or leg.expiry is None or leg.right is None:
        return None, None
    cached = _cached_option_chain(
        p.symbol, expected_underlier_con_id, options_store, chain_cache
    )
    if cached is None:
        return None, None
    chain_df, meta = cached
    if option_chain_freshness(meta.as_of, today)[1]:
        return None, None
    row = _find_quote_row(
        chain_df,
        expiry=leg.expiry,
        strike=leg.strike,
        right=leg.right,
        con_id=getattr(leg, "con_id", None),
    )
    if row is None:
        return None, None
    try:
        observed_at = datetime.fromisoformat(
            str(row.observed_at).replace("Z", "+00:00")
        )
    except ValueError:
        return None, None
    if option_chain_freshness(str(row.observed_at), today)[1]:
        return None, None
    mark = _quote_mark(row)
    return (mark, observed_at.date()) if mark is not None else (None, None)


def _priced_option_leg(
    p: Position,
    leg,
    options_store: OptionsStore,
    chain_cache: dict[str, tuple[pd.DataFrame, OptionsSnapshotMeta] | None],
    spot: float,
    as_of: date,
    expected_underlier_con_id: int | None,
) -> tuple[BookLeg, ExpiryLegOut, str] | None:
    """A single OPT position's book leg + expiry-bucket row, IF its strike/
    expiry has a matching cached-chain quote with a usable IV — else None
    (the caller folds that into the sleeve's honest-empty reason)."""
    if leg is None or leg.strike is None or leg.expiry is None or leg.right is None:
        return None
    cached = _cached_option_chain(
        p.symbol, expected_underlier_con_id, options_store, chain_cache
    )
    if cached is None:
        return None
    chain_df, meta = cached
    if option_chain_freshness(
        meta.as_of,
        as_of,
        stale_after_business_days=_OPTIONS_STALE_AFTER_DAYS,
    )[1]:
        return None
    row = _find_quote_row(
        chain_df,
        expiry=leg.expiry,
        strike=leg.strike,
        right=leg.right,
        con_id=getattr(leg, "con_id", None),
    )
    if row is None:
        return None
    if option_chain_freshness(
        str(row.observed_at),
        as_of,
        stale_after_business_days=_OPTIONS_STALE_AFTER_DAYS,
    )[1]:
        return None
    if _quote_mark(row) is None:
        return None
    iv = row.iv
    if iv is None or not np.isfinite(float(iv)) or float(iv) <= 0:
        return None
    iv = float(iv)
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
    return book_leg, bucket_row, meta.as_of


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


def _book_and_benchmark_returns(
    close_series: dict[int, pd.Series],
    market_values: dict[int, float],
    benchmark_series: pd.Series,
) -> tuple[pd.DataFrame, float] | None:
    """Weighted book and benchmark returns over one common price calendar.

    Book weights are fractions of GROSS |market value| — hedge.py's
    `_portfolio_returns` convention — plus the gross value the caller must use as the dollar
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
    benchmark_column = "__benchmark__"
    prices = pd.concat(
        {
            **{cid: close_series[cid] for cid in con_ids},
            benchmark_column: benchmark_series,
        },
        axis=1,
        sort=False,
    ).dropna()
    if len(prices) < 2:
        return None
    returns = prices.pct_change().dropna()
    if returns.empty:
        return None
    weights = np.array([market_values[cid] / gross for cid in con_ids])
    return (
        pd.concat(
            {
                "book": weighted_portfolio_returns(returns, con_ids, weights),
                "bench": returns[benchmark_column],
            },
            axis=1,
        ),
        gross,
    )


def _empty_attribution(reason: str, window_days: int) -> AttributionOut:
    return AttributionOut(
        available=False,
        reason=reason,
        window_days=window_days,
        beta=None,
        n_obs=0,
        total_pnl=None,
        core_pnl=None,
        overlay_pnl=None,
        core_share=None,
        overlay_share=None,
        series=[],
    )


def _compute_attribution(
    store,
    close_series: dict[int, pd.Series],
    market_values: dict[int, float],
    benchmark: str,
    symbol_map: dict[str, int],
    window_days: int,
    benchmark_series: pd.Series | None = None,
) -> AttributionOut:
    empty = lambda reason: _empty_attribution(reason, window_days)  # noqa: E731

    if benchmark not in symbol_map:
        return empty(f"benchmark {benchmark!r} not in cache")
    bench_series = (
        benchmark_series
        if benchmark_series is not None
        else _close_series(store, symbol_map[benchmark])
    )
    if bench_series is None:
        return empty(f"benchmark {benchmark!r} has no cached bars")

    result = _book_and_benchmark_returns(
        close_series, market_values, bench_series
    )
    if result is None:
        return empty(
            "no priced positions with enough common book/benchmark price history"
        )
    aligned, gross = result
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
    base_currency = request.app.state.base_currency

    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pinned_snapshot_id: str | None = None
    today = datetime.now(timezone.utc).date()

    if book_ref is not None:
        pinned = read_book(store, book_ref)
        validate_pinned_book_scope(request.app.state, pinned)
        valuation_ts = pinned["valuation_ts"]
        pinned_snapshot_id = pinned["snapshot_id"]
        pinned_positions = read_book_positions(store, book_ref)
        validate_pinned_instrument_identities(store, pinned, pinned_positions)
        portfolio, option_legs = _book_from_book_ref(
            store,
            book_ref,
            valuation_ts=valuation_ts,
            use_persisted_contract_ids=pinned["source"] == "live_ibkr",
        )
    elif broker is None and getattr(
        request.app.state, "broker_connection_error", None
    ) is not None:
        raise HTTPException(
            503,
            detail="live broker unavailable; reconnect IBKR or request a pinned book_ref",
        )
    elif broker is None:
        portfolio = Portfolio(positions=(), as_of=valuation_ts)
        option_legs = []
    else:
        portfolio = await broker.get_portfolio()
        validate_live_stock_identities(store, portfolio)
        # Positionally aligned with `portfolio.positions`, exactly like the
        # book_ref path. Complete IBKR option contracts can therefore use the
        # same cached-chain pricing seam; incomplete legacy contracts remain
        # `None` and degrade honestly below.
        option_legs = [
            ResolvedBookPosition(
                con_id=p.con_id,
                symbol=p.symbol,
                qty=p.qty,
                strike=p.strike,
                expiry=p.expiry,
                right=p.right,
                multiplier=p.multiplier,
            )
            if p.sec_type == "OPT"
            and option_terms_complete(strike=p.strike, expiry=p.expiry, right=p.right)
            else None
            for p in portfolio.positions
        ]

    symbol_map = store.read_symbol_map()
    instrument_metadata = read_instrument_metadata_map(store)
    resolved_position_currencies: list[str | None] = []
    for position in portfolio.positions:
        if position.currency and position.currency.strip():
            resolved_position_currencies.append(
                position.currency.strip().upper()
            )
            continue
        metadata = mapped_instrument_metadata(
            instrument_metadata, symbol_map, position.symbol
        )
        resolved_position_currencies.append(
            str(metadata.get("currency") or "").strip().upper() or None
        )
    account, account_note, account_fx_evidence = await _resolve_account(
        broker,
        book_ref,
        store=store,
        base_currency=base_currency,
        as_of=today,
    )
    unsupported_option_currencies = sorted(
        {
            currency or "UNKNOWN"
            for position, currency in zip(portfolio.positions, resolved_position_currencies)
            if position.sec_type == "OPT"
            and currency != base_currency
        }
    )
    if unsupported_option_currencies:
        raise HTTPException(
            422,
            detail=(
                "cross-currency option aggregation is not available until all "
                "Greeks and stress P&L are normalized leg-by-leg; unsupported "
                f"option currencies: {unsupported_option_currencies}"
            ),
        )
    unsupported_security_types = sorted(
        {
            position.sec_type
            for position in portfolio.positions
            if position.sec_type not in {"STK", "OPT"}
        }
    )
    if unsupported_security_types:
        raise HTTPException(
            422,
            detail=(
                "portfolio security types are not supported in this release: "
                f"{unsupported_security_types}"
            ),
        )

    snapshot_id = pinned_snapshot_id or BookSnapshot.create(
        portfolio, valuation_ts=valuation_ts, base_currency=base_currency
    ).snapshot_id
    position_currencies = {
        currency for currency in resolved_position_currencies if currency is not None
    }
    unknown_currency = any(currency is None for currency in resolved_position_currencies)
    missing_currencies: set[str] = {"UNKNOWN"} if unknown_currency else set()
    fx_converters: dict[str, FxConverter] = {}
    for currency in sorted(position_currencies - {base_currency}):
        try:
            fx_converters[currency] = FxConverter.from_store(
                store,
                base_currency=base_currency,
                currencies={currency},
            )
        except (FxConversionUnavailable, ValueError):
            missing_currencies.add(currency)

    # --- ledger essentials: price, cost basis, unrealized P&L ---
    avg_costs: dict[int, float] = {}
    if book_ref is None and broker is not None:
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
    options_store = OptionsStore(store.root)
    option_chain_cache: dict[str, tuple[pd.DataFrame, OptionsSnapshotMeta] | None] = {}
    close_series: dict[int, pd.Series] = {}
    base_close_series: dict[int, pd.Series] = {}
    missing_base_history_symbols: set[str] = set()
    last_closes: list[float | None] = []
    mark_dates: list[date | None] = []
    fx_rates: list[float | None] = []
    local_market_values: list[float | None] = []
    market_values: list[float | None] = []
    for p, leg, currency in zip(
        portfolio.positions, option_legs, resolved_position_currencies
    ):
        if p.con_id not in close_series:
            series = _close_series(store, p.con_id)
            if series is not None:
                close_series[p.con_id] = series
        mark_date = None
        if p.sec_type == "OPT":
            last_close, mark_date = _option_mark(
                p,
                leg,
                options_store,
                option_chain_cache,
                today,
                symbol_map.get(p.symbol),
            )
        else:
            last_close = (
                None
                if _series_is_stale(close_series.get(p.con_id), today)
                else _last_close(close_series.get(p.con_id))
            )
            if last_close is not None:
                mark_date = _last_observation_date(close_series.get(p.con_id))
        last_closes.append(last_close)
        if p.sec_type == "STK" and last_close is None:
            missing_base_history_symbols.add(p.symbol)
        mark_dates.append(mark_date)
        local_mv = (
            clean(p.qty * p.multiplier * last_close)
            if last_close is not None
            else None
        )
        local_market_values.append(local_mv)
        rate: float | None = None
        currency_converter = fx_converters.get(currency) if currency is not None else None
        if currency == base_currency and mark_date is not None:
            rate = 1.0
        elif (
            currency is not None
            and currency_converter is not None
            and mark_date is not None
        ):
            try:
                rate = clean(currency_converter.rate(currency, mark_date))
            except FxConversionUnavailable:
                missing_currencies.add(currency)
        fx_rates.append(rate)
        market_values.append(
            clean(local_mv * rate)
            if local_mv is not None and rate is not None
            else None
        )
        local_history = close_series.get(p.con_id)
        if local_history is None:
            if p.qty != 0:
                missing_base_history_symbols.add(p.symbol)
        elif currency == base_currency:
            base_close_series[p.con_id] = local_history
        elif currency is not None and currency_converter is not None:
            try:
                base_close_series[p.con_id] = currency_converter.convert_series(
                    local_history, currency
                )
            except FxConversionUnavailable:
                missing_base_history_symbols.add(p.symbol)
        else:
            missing_base_history_symbols.add(p.symbol)

    fx_sources = sorted({converter.source for converter in fx_converters.values()})
    fx_as_of_values = [
        converter.as_of
        for converter in fx_converters.values()
        if converter.as_of is not None
    ]
    fx_fetched_at_values = [
        converter.fetched_at
        for converter in fx_converters.values()
        if converter.fetched_at
    ]
    if missing_currencies:
        fx_status = FxEvidenceOut(
            status="incomplete",
            base_currency=base_currency,
            source=", ".join(fx_sources) or None,
            as_of=min(fx_as_of_values) if fx_as_of_values else None,
            fetched_at=(
                min(fx_fetched_at_values) if fx_fetched_at_values else None
            ),
            missing_currencies=sorted(missing_currencies),
            note=(
                f"Local marks are shown, but {base_currency} totals exclude positions "
                "without trustworthy dated FX. Run sync to refresh ECB reference rates."
            ),
        )
    elif fx_converters:
        fx_status = FxEvidenceOut(
            status="converted",
            base_currency=base_currency,
            source=", ".join(fx_sources) or None,
            as_of=min(fx_as_of_values) if fx_as_of_values else None,
            fetched_at=(
                min(fx_fetched_at_values) if fx_fetched_at_values else None
            ),
            missing_currencies=[],
            note=(
                f"Market values are normalized to {base_currency} with dated ECB "
                "reference rates; local unit prices are retained."
            ),
        )
    else:
        fx_status = FxEvidenceOut(
            status="identity",
            base_currency=base_currency,
            source=None,
            as_of=None,
            fetched_at=None,
            missing_currencies=[],
            note=f"All priced positions are already denominated in {base_currency}.",
        )
    fx_status = _merge_fx_evidence(
        fx_status,
        account_fx_evidence,
        base_currency=base_currency,
    )

    known_mvs = [mv for mv in market_values if mv is not None]
    priced_mv = sum(known_mvs) if known_mvs else None
    valuation_complete = bool(portfolio.positions) and len(known_mvs) == len(
        portfolio.positions
    )
    total_mv = priced_mv if valuation_complete else None

    positions_out: list[PositionOut] = []
    unrealized_values: list[float] = []
    for p, currency, last_close, mark_date, rate, local_mv, mv in zip(
        portfolio.positions,
        resolved_position_currencies,
        last_closes,
        mark_dates,
        fx_rates,
        local_market_values,
        market_values,
    ):
        avg_cost = clean(avg_costs.get(p.con_id))
        unrealized_local = (
            clean((last_close - avg_cost) * p.qty * p.multiplier)
            if last_close is not None and avg_cost is not None
            else None
        )
        # IBKR avgCost is in the instrument's local quote currency. Current
        # FX can normalize today's market value, but it cannot reconstruct
        # acquisition-date base cost or the FX gain/loss on invested capital.
        # Until lot-level historical FX (or broker base P&L) is available,
        # expose foreign P&L only in local currency and withhold the base total.
        unrealized = unrealized_local if currency == base_currency else None
        if unrealized is not None:
            unrealized_values.append(unrealized)
        positions_out.append(
            PositionOut(
                con_id=p.con_id,
                symbol=p.symbol,
                qty=p.qty,
                sec_type=p.sec_type,
                multiplier=p.multiplier,
                exchange=p.exchange,
                strike=p.strike,
                expiry=p.expiry,
                right=p.right,
                currency=currency,
                last_close=last_close,
                mark_as_of=mark_date.isoformat() if mark_date is not None else None,
                fx_rate_to_base=rate,
                local_market_value=local_mv,
                market_value=mv,
                weight=(mv / total_mv if valuation_complete and mv is not None and total_mv else None),
                avg_cost=avg_cost,
                unrealized_pnl_local=unrealized_local,
                unrealized_pnl=unrealized,
            )
        )

    pnl_complete = bool(portfolio.positions) and len(unrealized_values) == len(
        portfolio.positions
    )
    reported_unrealized = sum(unrealized_values) if unrealized_values else None
    totals = Totals(
        market_value=total_mv,
        priced_market_value=priced_mv,
        n_positions=len(portfolio.positions),
        priced_positions=len(known_mvs),
        valuation_status=(
            "empty"
            if not portfolio.positions
            else "complete"
            if valuation_complete
            else "partial"
        ),
        unrealized_pnl=reported_unrealized if pnl_complete else None,
        reported_unrealized_pnl=reported_unrealized,
        pnl_status=(
            "empty"
            if not portfolio.positions
            else "complete"
            if pnl_complete
            else "partial"
        ),
    )

    # --- delta-adjusted exposure + options sleeve + expiry buckets ---
    symbol_currencies: dict[str, str | None] = {}
    for position, currency in zip(
        portfolio.positions, resolved_position_currencies
    ):
        symbol_currencies.setdefault(position.symbol, currency)

    benchmark_local_series = (
        _close_series(store, symbol_map[benchmark]) if benchmark in symbol_map else None
    )
    benchmark_base_series: pd.Series | None = None
    benchmark_unavailability_reason: str | None = None
    benchmark_identity_reason: str | None = None
    try:
        benchmark_metadata = mapped_instrument_metadata(
            instrument_metadata, symbol_map, benchmark
        )
    except HTTPException as exc:
        benchmark_metadata = {}
        benchmark_identity_reason = f"benchmark {benchmark} {exc.detail}"
    benchmark_currency = benchmark_metadata.get("currency")
    if benchmark_local_series is None:
        benchmark_unavailability_reason = f"benchmark {benchmark} has no cached bars"
    elif _series_is_stale(benchmark_local_series, today):
        benchmark_unavailability_reason = f"benchmark {benchmark} cached bars are stale"
    elif benchmark_identity_reason is not None:
        benchmark_unavailability_reason = benchmark_identity_reason
    elif not benchmark_currency:
        benchmark_unavailability_reason = (
            f"benchmark {benchmark} currency metadata unavailable"
        )
    elif benchmark_currency == base_currency:
        benchmark_base_series = benchmark_local_series
    else:
        # The position converter is intentionally scoped to held currencies.
        # A foreign benchmark can be a different currency, so construct an
        # evidence set that explicitly includes it instead of reusing a
        # converter that may not have loaded the required quote.
        try:
            analysis_converter = FxConverter.from_store(
                store,
                base_currency=base_currency,
                currencies={benchmark_currency},
            )
        except (FxConversionUnavailable, ValueError):
            analysis_converter = None
        if analysis_converter is not None:
            try:
                benchmark_base_series = analysis_converter.convert_series(
                    benchmark_local_series, benchmark_currency
                )
            except FxConversionUnavailable:
                benchmark_unavailability_reason = (
                    f"benchmark {benchmark} FX evidence unavailable"
                )
        else:
            benchmark_unavailability_reason = (
                f"benchmark {benchmark} FX evidence unavailable"
            )

    groups: dict[str, list[tuple[Position, object | None]]] = {}
    for p, leg in zip(portfolio.positions, option_legs):
        groups.setdefault(p.symbol, []).append((p, leg))

    total_option_positions = sum(p.sec_type == "OPT" for p in portfolio.positions)
    has_option_positions = total_option_positions > 0
    priced_option_positions = 0
    chain_snapshots: list[tuple[str, int | None, bool]] = []
    book_legs: list[BookLeg] = []
    priced_option_underliers: set[str] = set()
    # Option underliers dropped BEFORE the chain lookup (no symbol-map entry
    # or no cached bars) — the sleeve's honest-empty reason must name them
    # rather than blame the chain (fix-round-1 minor: an unknown-symbol OPT
    # leg used to fall through to the generic "chain not ingested" reason).
    unpriceable_option_underliers: set[str] = set()
    expiry_rows: list[ExpiryLegOut] = []
    underlier_betas: dict[str, float | None] = {}
    underlier_fx_rates: dict[str, float | None] = {}

    for underlier, group in groups.items():
        group_has_options = any(p.sec_type == "OPT" for p, _ in group)
        if underlier not in symbol_map:
            if group_has_options:
                unpriceable_option_underliers.add(underlier)
            continue
        cached_chain = (
            _cached_option_chain(
                underlier,
                symbol_map.get(underlier),
                options_store,
                option_chain_cache,
            )
            if group_has_options
            else None
        )
        if cached_chain is not None:
            chain_df = cached_chain[0]
            for position, leg in group:
                if (
                    position.sec_type != "OPT"
                    or leg is None
                    or leg.strike is None
                    or leg.expiry is None
                    or leg.right is None
                ):
                    continue
                quote_row = _find_quote_row(
                    chain_df,
                    expiry=leg.expiry,
                    strike=leg.strike,
                    right=leg.right,
                    con_id=getattr(leg, "con_id", None),
                )
                if quote_row is None:
                    continue
                quote_as_of = str(quote_row.observed_at)
                age_days, stale = option_chain_freshness(
                    quote_as_of,
                    today,
                    stale_after_business_days=_OPTIONS_STALE_AFTER_DAYS,
                )
                chain_snapshots.append((quote_as_of, age_days, stale))
        spot_series = _close_series(store, symbol_map[underlier])
        # Exposure, Greeks, and stress P&L are just as mark-sensitive as the
        # ledger. Reuse the ledger freshness contract so a fresh chain cannot
        # revive an arbitrarily old underlier spot.
        spot_is_stale = _series_is_stale(spot_series, today)
        spot = None if spot_is_stale else _last_close(spot_series)
        spot_date = None if spot_is_stale else _last_observation_date(spot_series)
        underlier_currency = symbol_currencies.get(underlier)
        underlier_rate: float | None = None
        if underlier_currency == base_currency and spot_date is not None:
            underlier_rate = 1.0
        elif (
            underlier_currency is not None
            and underlier_currency in fx_converters
            and spot_date is not None
        ):
            try:
                underlier_rate = clean(
                    fx_converters[underlier_currency].rate(
                        underlier_currency, spot_date
                    )
                )
            except FxConversionUnavailable:
                underlier_rate = None
        underlier_fx_rates[underlier] = underlier_rate
        if spot is None or underlier_rate is None:
            if group_has_options:
                unpriceable_option_underliers.add(underlier)
            continue

        risk_series = base_close_series.get(symbol_map[underlier])
        if risk_series is None:
            risk_series = spot_series if underlier_rate == 1.0 else None

        if underlier == benchmark:
            beta: float | None = 1.0
        else:
            beta = (
                _last_beta(risk_series, benchmark_base_series)
                if risk_series is not None and benchmark_base_series is not None
                else None
            )
        underlier_betas[underlier] = beta

        for p, leg in group:
            if p.sec_type != "OPT":
                book_legs.append(BookLeg(underlier=underlier, qty=p.qty, is_option=False, spot=spot, r=_RISK_FREE_RATE))
                continue
            resolved = _priced_option_leg(
                p,
                leg,
                options_store,
                option_chain_cache,
                spot,
                today,
                symbol_map.get(underlier),
            )
            if resolved is None:
                continue
            book_leg, bucket_row, _ = resolved
            book_legs.append(book_leg)
            expiry_rows.append(bucket_row)
            priced_option_underliers.add(underlier)
            priced_option_positions += 1

    betas_clean = {k: v for k, v in underlier_betas.items() if v is not None}
    underlyings = compute_book_greeks(book_legs, betas=betas_clean) if book_legs else []

    exposure_out = [
        UnderlyingExposureOut(
            underlier=u.underlier,
            currency=symbol_currencies.get(u.underlier),
            fx_rate_to_base=clean(underlier_fx_rates.get(u.underlier)),
            spot=clean(u.spot),
            net_delta=clean(u.delta),
            dollar_delta=clean(
                u.dollar_delta * underlier_fx_rates[u.underlier]
                if underlier_fx_rates.get(u.underlier) is not None
                else None
            ),
            beta=clean(underlier_betas.get(u.underlier)),
            spy_equivalent_notional=clean(
                u.spy_equivalent_notional * underlier_fx_rates[u.underlier]
                if u.spy_equivalent_notional is not None
                and underlier_fx_rates.get(u.underlier) is not None
                else None
            ),
            beta_note=(
                None
                if underlier_betas.get(u.underlier) is not None
                else benchmark_unavailability_reason
                or f"insufficient history for beta vs {benchmark} ({_BETA_WINDOW}d window)"
            ),
        )
        for u in underlyings
    ]

    missing_option_positions = total_option_positions - priced_option_positions
    if chain_snapshots:
        oldest_snapshot = max(
            chain_snapshots,
            key=lambda item: item[1] if item[1] is not None else math.inf,
        )
        chain_as_of, chain_age_days, _ = oldest_snapshot
        chain_stale: bool | None = any(item[2] for item in chain_snapshots)
    else:
        chain_as_of = None
        chain_age_days = None
        chain_stale = None

    if not has_option_positions:
        options_sleeve = OptionsSleeveOut(
            available=False,
            status="unavailable",
            total_positions=0,
            priced_positions=0,
            missing_positions=0,
            chain_as_of=None,
            chain_age_days=None,
            chain_stale=None,
            reason="no option positions",
            underlyings=[],
            stress_grid=None,
        )
    elif not priced_option_underliers:
        if chain_stale is True:
            reason = "cached option chain is stale — run options_sync"
        elif unpriceable_option_underliers:
            reason = (
                "option underliers are unavailable, stale, or missing trustworthy "
                f"FX: {sorted(unpriceable_option_underliers)} — sync bars first"
            )
        else:
            reason = "chain not ingested — run options_sync"
        options_sleeve = OptionsSleeveOut(
            available=False,
            status="unavailable",
            total_positions=total_option_positions,
            priced_positions=0,
            missing_positions=missing_option_positions,
            chain_as_of=chain_as_of,
            chain_age_days=chain_age_days,
            chain_stale=chain_stale,
            reason=reason,
            underlyings=[],
            stress_grid=None,
        )
    else:
        sleeve_legs = [leg for leg in book_legs if leg.underlier in priced_option_underliers]
        grid = aggregate_book_stress_grid(sleeve_legs)
        status: Literal["complete", "partial"] = (
            "complete"
            if missing_option_positions == 0 and chain_stale is not True
            else "partial"
        )
        options_sleeve = OptionsSleeveOut(
            available=True,
            status=status,
            total_positions=total_option_positions,
            priced_positions=priced_option_positions,
            missing_positions=missing_option_positions,
            chain_as_of=chain_as_of,
            chain_age_days=chain_age_days,
            chain_stale=chain_stale,
            reason=(
                None
                if status == "complete"
                else (
                    "cached option chain is stale"
                    if chain_stale is True and missing_option_positions == 0
                    else (
                        f"{missing_option_positions} of {total_option_positions} option positions "
                        "could not be priced from cached chains"
                    )
                )
            ),
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

    if has_option_positions:
        attribution = _empty_attribution(
            "historical attribution unavailable for option books without option price history",
            attribution_days,
        )
    elif missing_base_history_symbols:
        attribution = _empty_attribution(
            "historical attribution unavailable because base-currency history "
            f"is missing for {sorted(missing_base_history_symbols)}",
            attribution_days,
        )
    elif benchmark_base_series is None:
        attribution = _empty_attribution(
            benchmark_unavailability_reason
            or f"benchmark {benchmark} evidence unavailable",
            attribution_days,
        )
    else:
        attribution = _compute_attribution(
            store,
            base_close_series,
            mv_by_conid,
            benchmark,
            symbol_map,
            attribution_days,
            benchmark_series=benchmark_base_series,
        )

    return PortfolioResponse(
        snapshot_id=snapshot_id,
        valuation_ts=valuation_ts,
        market_data_as_of=(
            min(mark_date for mark_date in mark_dates if mark_date is not None).isoformat()
            if any(mark_date is not None for mark_date in mark_dates)
            else None
        ),
        base_currency=base_currency,
        fx=fx_status,
        positions=positions_out,
        totals=totals,
        account=account,
        account_note=account_note,
        exposure=exposure_out,
        options_sleeve=options_sleeve,
        expiry_buckets=expiry_buckets,
        attribution=attribution,
    )
