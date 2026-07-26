"""Macro domain routes: yields/curve, Fed net liquidity, sector & factor
rotation — all read straight from cached named series / bars (Global
Constraints: store-only, never network, never a 500).

GET /api/macro composes independent blocks (yields, curve, net_liquidity,
sectors, factors, regime_rotation, sensitivity). Any block whose backing
series/bars aren't cached is *omitted* (null) rather than failing the whole
response; the yields/curve blocks in particular need all three named rates
for the 2s10s spread arithmetic to be honest, so they're all-or-nothing.
Sector/factor rows are independent — a symbol missing from the cache is
simply dropped from that list. Every series/symbol that couldn't be sourced
is named in the top-level `missing` list so the page can say exactly what a
sync would fix.

Wave-3B "Macro book-aware" additions:

* `sensitivity` — the amber column: the pinned book's estimated dollar
  response to a standard shock of each macro driver (rates +10bp, each
  sector/factor ETF +1%, VIX +5 vol pts), from a 252-day daily regression
  with Newey-West HAC CIs (pure math: quantmind.exposure.sensitivity;
  Global Constraint: every estimate carries a CI/SE). Requires `?book_ref=`
  (a pinned snapshot, wave-3 Task A1's spine): no book -> `sensitivity` is
  null and the page says "pin a book to see sensitivities"; an unknown or
  malformed ref is a structured 422 (routers/book.py's policy), never a 500.
  Option legs and unpriceable symbols are named in `excluded` rather than
  silently mispriced as shares.
* `curve` — today's US3M/US2Y/US10Y curve vs its 21- and 63-trading-day-ago
  snapshots, 2s10s spread per snapshot.
* `regime_rotation` — the rotation universe's mean daily return (+ SE)
  conditioned on VIX-close terciles: "in high-vol regimes, what led/lagged".
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import clean, downsample, iso, weighted_portfolio_returns
from quantmind.api.routers.book import read_book_positions
from quantmind.exposure.sensitivity import (
    Shock,
    book_shock_sensitivity,
    rate_shock,
    regime_conditional_returns,
    return_shock,
    shock_factor,
    vol_shock,
)
from quantmind.risk.returns import InsufficientDataError

router = APIRouter()

_MAX_SERIES_POINTS = 500

# v1 rotation universe (matches sync_cli.DEFAULT_UNIVERSE's sector/factor tail).
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB"]
FACTORS = ["MTUM", "VLUE", "QUAL", "USMV"]

# response key -> store series name (FRED rates are cached as decimals, see
# quantmind.sources.fred.FRED_STORE_SERIES).
_YIELD_SERIES = {"us10y": "US10Y", "us2y": "US2Y", "us3m": "US3M"}

_ONE_MONTH = 21  # trading days
_THREE_MONTH = 63

# Curve tenors in maturity order: (store name, response key, years to maturity).
_CURVE_TENORS = [("US3M", "us3m", 0.25), ("US2Y", "us2y", 2.0), ("US10Y", "us10y", 10.0)]

# The volatility regime/shock instrument (IBKR Index bars via wave-3A A2).
VOL_SYMBOL = "VIX"

# Sensitivity regression window (trading days) — labeled in every response.
_SENS_WINDOW = 252
_SENS_WINDOW_NOTE = (
    f"last {_SENS_WINDOW} aligned daily returns (or fewer); "
    "Newey-West HAC SEs, 95% CI; linear (delta) approximation"
)


class SeriesPoint(BaseModel):
    date: str
    value: float | None


class YieldsBlock(BaseModel):
    us10y: float | None
    us2y: float | None
    us3m: float | None
    spread_2s10s: float | None
    series: dict[str, list[SeriesPoint]]


class CurveTenor(BaseModel):
    tenor: str  # store name: US3M / US2Y / US10Y
    years: float  # maturity in years (chart x-axis)
    today: float | None
    m1: float | None  # 21 trading days ago
    m3: float | None  # 63 trading days ago


class CurveBlock(BaseModel):
    tenors: list[CurveTenor]  # maturity order (US3M, US2Y, US10Y)
    spread_2s10s_today: float | None
    spread_2s10s_m1: float | None
    spread_2s10s_m3: float | None
    note: str


class NetLiquidityBlock(BaseModel):
    latest_bn: float | None
    series: list[SeriesPoint]
    cadence_note: str


class RotationRow(BaseModel):
    symbol: str
    ret_1d: float | None
    ret_1m: float | None
    ret_3m: float | None


class RegimeSymbolStat(BaseModel):
    symbol: str
    mean_daily: float | None
    se_daily: float | None  # SE of the mean (null when the bucket has 1 day)


class RegimeBucketOut(BaseModel):
    bucket: str  # "low" / "mid" / "high"
    lo: float | None  # observed regime-variable range inside the bucket
    hi: float | None
    n_days: int
    rows: list[RegimeSymbolStat]  # ranked by mean daily return desc


class RegimeRotationBlock(BaseModel):
    regime_note: str
    buckets: list[RegimeBucketOut]
    as_of: str | None
    note: str | None = None  # set when conditioning was refused (insufficient data)


class SensitivityRow(BaseModel):
    driver: str
    group: str  # "rates" | "sectors" | "factors" | "vol"
    shock_label: str  # e.g. "+10bp" / "+1%" / "+5 vol pts"
    dollar_response: float | None
    se: float | None
    ci_low: float | None
    ci_high: float | None
    beta: float | None
    n_obs: int | None
    note: str | None = None  # set when this row's estimate was refused


class SensitivityBlock(BaseModel):
    book_ref: str
    book_gross: float | None
    excluded: list[str]  # legs not priced into the book series, with reasons
    rows: list[SensitivityRow]
    window_note: str
    as_of: str | None
    note: str | None = None  # set when no rows could be computed at all


class MacroResponse(BaseModel):
    yields: YieldsBlock | None
    curve: CurveBlock | None
    net_liquidity: NetLiquidityBlock | None
    sectors: list[RotationRow]
    factors: list[RotationRow]
    regime_rotation: RegimeRotationBlock | None
    sensitivity: SensitivityBlock | None
    as_of: str | None
    missing: list[str]
    # Batch-2 final review item 4: set when a well-formed but UNKNOWN
    # book_ref was posted — the sensitivity column degrades to null with
    # this recovery note instead of 422ing the whole market page.
    note: str | None = None


def _series_points(series: pd.Series, max_points: int) -> list[SeriesPoint]:
    ds = downsample(series, max_points)
    return [SeriesPoint(date=iso(d), value=clean(v)) for d, v in ds.items()]


def _read_named_series(store, name: str) -> pd.Series | None:
    try:
        s = store.read_series(name)
    except FileNotFoundError:
        return None
    return s if len(s) > 0 else None


def _rotation_row(
    store, symbol_map: dict[str, int], symbol: str
) -> tuple[RotationRow | None, pd.Timestamp | None, pd.Series | None]:
    con_id = symbol_map.get(symbol)
    if con_id is None:
        return None, None, None
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        return None, None, None
    close = bars["close"]
    if len(close) == 0:
        return None, None, None
    ret_1d = clean(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else None
    ret_1m = clean(close.iloc[-1] / close.iloc[-1 - _ONE_MONTH] - 1) if len(close) > _ONE_MONTH else None
    ret_3m = clean(close.iloc[-1] / close.iloc[-1 - _THREE_MONTH] - 1) if len(close) > _THREE_MONTH else None
    row = RotationRow(symbol=symbol, ret_1d=ret_1d, ret_1m=ret_1m, ret_3m=ret_3m)
    return row, close.index[-1], close


def _rotation_block(
    store, symbol_map: dict[str, int], symbols: list[str], missing: list[str]
) -> tuple[list[RotationRow], pd.Timestamp | None, dict[str, pd.Series]]:
    rows: list[RotationRow] = []
    closes: dict[str, pd.Series] = {}
    latest: pd.Timestamp | None = None
    for symbol in symbols:
        row, last_date, close = _rotation_row(store, symbol_map, symbol)
        if row is None:
            missing.append(symbol)
            continue
        rows.append(row)
        if close is not None:
            closes[symbol] = close
        if last_date is not None and (latest is None or last_date > latest):
            latest = last_date
    # Rotation ranking: best 1-day movers first, symbols with no computable
    # ret_1d (too little history) sink to the bottom rather than sorting
    # arbitrarily.
    rows.sort(key=lambda r: (r.ret_1d is None, -(r.ret_1d if r.ret_1d is not None else 0.0)))
    return rows, latest, closes


def _lagged(series: pd.Series, lag: int) -> float | None:
    """Value `lag` trading days before the latest observation; null when the
    cached history is too short (never a 500)."""
    return clean(series.iloc[-1 - lag]) if len(series) > lag else None


def _curve_block(yield_series: dict[str, pd.Series]) -> CurveBlock | None:
    """Today's curve vs its 21/63-trading-day-ago snapshots. Same all-or-
    nothing rule as the yields block: partial tenors would make the snapshot
    comparison (and the 2s10s spread) dishonest."""
    if len(yield_series) != len(_YIELD_SERIES):
        return None
    tenors: list[CurveTenor] = []
    values: dict[str, dict[str, float | None]] = {}
    for name, key, years in _CURVE_TENORS:
        s = yield_series[key]
        v = {"today": clean(s.iloc[-1]), "m1": _lagged(s, _ONE_MONTH), "m3": _lagged(s, _THREE_MONTH)}
        values[key] = v
        tenors.append(CurveTenor(tenor=name, years=years, today=v["today"], m1=v["m1"], m3=v["m3"]))

    def spread(snapshot: str) -> float | None:
        a, b = values["us10y"][snapshot], values["us2y"][snapshot]
        return clean(a - b) if a is not None and b is not None else None

    return CurveBlock(
        tenors=tenors,
        spread_2s10s_today=spread("today"),
        spread_2s10s_m1=spread("m1"),
        spread_2s10s_m3=spread("m3"),
        note=f"snapshots: today vs {_ONE_MONTH} and {_THREE_MONTH} trading days ago",
    )


def _vix_close(store, symbol_map: dict[str, int]) -> pd.Series | None:
    con_id = symbol_map.get(VOL_SYMBOL)
    if con_id is None:
        return None
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        return None
    close = bars["close"]
    return close if len(close) > 0 else None


def _regime_block(closes: dict[str, pd.Series], vix_close: pd.Series) -> RegimeRotationBlock:
    regime_note = f"{VOL_SYMBOL} close terciles over the shared daily sample"
    returns_df = pd.DataFrame({s: c.pct_change().dropna() for s, c in closes.items()}).dropna()
    try:
        stats = regime_conditional_returns(returns_df, vix_close, n_buckets=3)
    except InsufficientDataError as e:
        return RegimeRotationBlock(regime_note=regime_note, buckets=[], as_of=None, note=str(e))

    buckets: list[RegimeBucketOut] = []
    for b in stats:
        ranked = sorted(
            returns_df.columns,
            key=lambda s: (
                clean(b.mean_daily[s]) is None,
                -(clean(b.mean_daily[s]) or 0.0),
            ),
        )
        buckets.append(
            RegimeBucketOut(
                bucket=b.bucket,
                lo=clean(b.lo),
                hi=clean(b.hi),
                n_days=b.n_days,
                rows=[
                    RegimeSymbolStat(
                        symbol=s, mean_daily=clean(b.mean_daily[s]), se_daily=clean(b.se_daily[s])
                    )
                    for s in ranked
                ],
            )
        )
    as_of = iso(returns_df.index[-1]) if len(returns_df) else None
    return RegimeRotationBlock(regime_note=regime_note, buckets=buckets, as_of=as_of)


def _empty_sensitivity(book_ref: str, excluded: list[str], note: str) -> SensitivityBlock:
    return SensitivityBlock(
        book_ref=book_ref, book_gross=None, excluded=excluded, rows=[],
        window_note=_SENS_WINDOW_NOTE, as_of=None, note=note,
    )


def _sensitivity_block(
    store,
    symbol_map: dict[str, int],
    book_ref: str,
    drivers: list[tuple[str, Shock, pd.Series]],
) -> SensitivityBlock:
    """The amber column's data: dollar response of the pinned book to each
    driver's standard shock. Never a 500 past ref resolution: unpriceable
    legs land in `excluded`, refused regressions land per-row in `note`."""
    legs = read_book_positions(store, book_ref)  # unknown/corrupt ref -> structured 422

    excluded: list[str] = []
    qtys: dict[str, float] = {}
    for p in legs:
        if p.right is not None:
            excluded.append(f"{p.symbol} (option leg — linear sensitivities cover equity/ETF legs only)")
            continue
        qtys[p.symbol] = qtys.get(p.symbol, 0.0) + p.qty

    closes: dict[str, pd.Series] = {}
    for symbol in qtys:
        con_id = symbol_map.get(symbol)
        if con_id is None:
            excluded.append(f"{symbol} (not in symbol map)")
            continue
        try:
            bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
        except FileNotFoundError:
            excluded.append(f"{symbol} (no cached bars)")
            continue
        close = bars["close"]
        if close.empty:
            excluded.append(f"{symbol} (no cached history)")
            continue
        closes[symbol] = close

    # Batch-2 final review item 7d: the inner join below truncates the whole
    # aligned window to the OLDEST last bar among the legs — name that
    # culprit in the as-of/window note rather than silently serving a stale
    # as_of with no explanation.
    window_note = _SENS_WINDOW_NOTE
    if closes:
        last_dates = {s: c.index[-1] for s, c in closes.items()}
        culprit = min(last_dates, key=last_dates.get)
        if last_dates[culprit] < max(last_dates.values()):
            window_note += (
                f"; book pricing as-of limited by {culprit} "
                f"(last bar {last_dates[culprit].strftime('%Y-%m-%d')})"
            )

    if not closes:
        return _empty_sensitivity(book_ref, excluded, "no priceable legs in this book — sync bars or re-pin")

    symbols = list(closes)
    prices = pd.concat(closes, axis=1).dropna()
    if prices.empty:
        return _empty_sensitivity(book_ref, excluded, "book legs share no overlapping cached history")

    last = prices.iloc[-1]
    market_values = {s: qtys[s] * float(last[s]) for s in symbols}
    gross = sum(abs(v) for v in market_values.values())
    if gross == 0:
        return _empty_sensitivity(book_ref, excluded, "book has zero gross market value")

    weights = np.array([market_values[s] / gross for s in symbols])
    returns = prices.pct_change().dropna()
    book_returns = weighted_portfolio_returns(returns, symbols, weights).tail(_SENS_WINDOW)

    rows: list[SensitivityRow] = []
    for group, shock, series in drivers:
        try:
            factor = shock_factor(series, shock.kind)
            est = book_shock_sensitivity(book_returns, factor, shock, book_gross=gross)
        except InsufficientDataError as e:
            rows.append(
                SensitivityRow(
                    driver=shock.driver, group=group, shock_label=shock.label,
                    dollar_response=None, se=None, ci_low=None, ci_high=None,
                    beta=None, n_obs=None, note=str(e),
                )
            )
            continue
        rows.append(
            SensitivityRow(
                driver=est.driver,
                group=group,
                shock_label=est.shock_label,
                dollar_response=clean(est.dollar_response),
                se=clean(est.se),
                ci_low=clean(est.ci[0]),
                ci_high=clean(est.ci[1]),
                beta=clean(est.beta),
                n_obs=est.n_obs,
            )
        )

    as_of = iso(book_returns.index[-1]) if len(book_returns) else None
    return SensitivityBlock(
        book_ref=book_ref,
        book_gross=clean(gross),
        excluded=excluded,
        rows=rows,
        window_note=window_note,
        as_of=as_of,
    )


@router.get("/macro", response_model=MacroResponse)
def macro(
    request: Request,
    # book_ref is client-controlled and resolves to a filesystem path in
    # routers/book.py — the 12-hex-char snapshot-id shape is enforced HERE
    # too (Field-bounds law) so a malformed ref 422s before any lookup.
    book_ref: str | None = Query(
        None,
        pattern="^[0-9a-f]{12}$",
        description="pinned book snapshot id — enables the book-sensitivity column",
    ),
) -> MacroResponse:
    store = request.app.state.store
    missing: list[str] = []
    latest_dates: list[pd.Timestamp] = []

    # Yields block: the 2s10s spread needs all three named rates, so a
    # partial set omits the whole block rather than serving a spread computed
    # from stale/missing legs.
    yield_series: dict[str, pd.Series] = {}
    for key, name in _YIELD_SERIES.items():
        s = _read_named_series(store, name)
        if s is None:
            missing.append(name)
        else:
            yield_series[key] = s

    yields_block: YieldsBlock | None = None
    if len(yield_series) == len(_YIELD_SERIES):
        us10y = clean(yield_series["us10y"].iloc[-1])
        us2y = clean(yield_series["us2y"].iloc[-1])
        us3m = clean(yield_series["us3m"].iloc[-1])
        spread = clean(us10y - us2y) if us10y is not None and us2y is not None else None
        yields_block = YieldsBlock(
            us10y=us10y,
            us2y=us2y,
            us3m=us3m,
            spread_2s10s=spread,
            series={k: _series_points(s, _MAX_SERIES_POINTS) for k, s in yield_series.items()},
        )
        latest_dates.extend(s.index[-1] for s in yield_series.values())

    curve_block = _curve_block(yield_series)

    # Net liquidity block.
    net_liquidity_block: NetLiquidityBlock | None = None
    nl = _read_named_series(store, "NET_LIQUIDITY")
    if nl is None:
        missing.append("NET_LIQUIDITY")
    else:
        net_liquidity_block = NetLiquidityBlock(
            latest_bn=clean(nl.iloc[-1]),
            series=_series_points(nl, _MAX_SERIES_POINTS),
            cadence_note="weekly",
        )
        latest_dates.append(nl.index[-1])

    symbol_map = store.read_symbol_map()
    sectors, sectors_latest, sector_closes = _rotation_block(store, symbol_map, SECTORS, missing)
    factors, factors_latest, factor_closes = _rotation_block(store, symbol_map, FACTORS, missing)
    if sectors_latest is not None:
        latest_dates.append(sectors_latest)
    if factors_latest is not None:
        latest_dates.append(factors_latest)

    vix_close = _vix_close(store, symbol_map)
    if vix_close is None:
        missing.append(VOL_SYMBOL)
    else:
        latest_dates.append(vix_close.index[-1])

    # Regime-conditional rotation: rotation universe conditioned on VIX terciles.
    regime_block: RegimeRotationBlock | None = None
    universe_closes = {**sector_closes, **factor_closes}
    if vix_close is not None and universe_closes:
        regime_block = _regime_block(universe_closes, vix_close)

    # Book sensitivity (the amber column) — only with a pinned book.
    sensitivity_block: SensitivityBlock | None = None
    top_note: str | None = None
    if book_ref is not None:
        drivers: list[tuple[str, Shock, pd.Series]] = []
        for key, name in (("us10y", "US10Y"), ("us2y", "US2Y")):
            if key in yield_series:
                drivers.append(("rates", rate_shock(name), yield_series[key]))
        # Batch-2 final review item 7b: US3M has no standard shock driver
        # (documented narrowing) — its row below says so explicitly rather
        # than leaving the tenor silently absent from the strip.
        for symbol in SECTORS:
            if symbol in sector_closes:
                drivers.append(("sectors", return_shock(symbol), sector_closes[symbol]))
        for symbol in FACTORS:
            if symbol in factor_closes:
                drivers.append(("factors", return_shock(symbol), factor_closes[symbol]))
        if vix_close is not None:
            drivers.append(("vol", vol_shock(VOL_SYMBOL), vix_close))
        try:
            sensitivity_block = _sensitivity_block(store, symbol_map, book_ref, drivers)
        except HTTPException as e:
            # Batch-2 final review item 4: a well-formed but UNKNOWN ref
            # (stale bookmark, cleared pins) must not 422 the whole market
            # page — degrade the sensitivity column with a recovery note.
            # Every other 422 (corrupted snapshot, partial legacy legs)
            # still propagates: those need the user to see the real error.
            if isinstance(e.detail, str) and e.detail.startswith("unknown book_ref"):
                sensitivity_block = None
                top_note = (
                    f"unknown book_ref {book_ref!r} — re-pin from What-If or Portfolio"
                )
            else:
                raise
        if sensitivity_block is not None and sensitivity_block.rows and "us3m" in yield_series:
            sensitivity_block.rows.append(
                SensitivityRow(
                    driver="US3M", group="rates", shock_label="—",
                    dollar_response=None, se=None, ci_low=None, ci_high=None,
                    beta=None, n_obs=None,
                    note=(
                        "no standard shock driver applies to the 3M tenor "
                        "(curve display only) — rate shocks cover US10Y/US2Y"
                    ),
                )
            )

    as_of = iso(max(latest_dates)) if latest_dates else None

    return MacroResponse(
        yields=yields_block,
        curve=curve_block,
        net_liquidity=net_liquidity_block,
        sectors=sectors,
        factors=factors,
        regime_rotation=regime_block,
        sensitivity=sensitivity_block,
        as_of=as_of,
        missing=missing,
        note=top_note,
    )
