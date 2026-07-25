"""Macro domain routes: yields/curve, Fed net liquidity, sector & factor
rotation — all read straight from cached named series / bars (Global
Constraints: store-only, never network, never a 500).

GET /api/macro composes four independent blocks (yields, net_liquidity,
sectors, factors). Any block whose backing series/bars aren't cached is
*omitted* (null) rather than failing the whole response; the yields block in
particular needs all three named rates for the 2s10s spread arithmetic to be
honest, so it's all-or-nothing. Sector/factor rows are independent — a symbol
missing from the cache is simply dropped from that list. Every series/symbol
that couldn't be sourced is named in the top-level `missing` list so the page
can say exactly what a sync would fix.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import clean, downsample, iso

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


class SeriesPoint(BaseModel):
    date: str
    value: float | None


class YieldsBlock(BaseModel):
    us10y: float | None
    us2y: float | None
    us3m: float | None
    spread_2s10s: float | None
    series: dict[str, list[SeriesPoint]]


class NetLiquidityBlock(BaseModel):
    latest_bn: float | None
    series: list[SeriesPoint]
    cadence_note: str


class RotationRow(BaseModel):
    symbol: str
    ret_1d: float | None
    ret_1m: float | None
    ret_3m: float | None


class MacroResponse(BaseModel):
    yields: YieldsBlock | None
    net_liquidity: NetLiquidityBlock | None
    sectors: list[RotationRow]
    factors: list[RotationRow]
    as_of: str | None
    missing: list[str]


def _series_points(series: pd.Series, max_points: int) -> list[SeriesPoint]:
    ds = downsample(series, max_points)
    return [SeriesPoint(date=iso(d), value=clean(v)) for d, v in ds.items()]


def _read_named_series(store, name: str) -> pd.Series | None:
    try:
        s = store.read_series(name)
    except FileNotFoundError:
        return None
    return s if len(s) > 0 else None


def _rotation_row(store, symbol_map: dict[str, int], symbol: str) -> tuple[RotationRow | None, pd.Timestamp | None]:
    con_id = symbol_map.get(symbol)
    if con_id is None:
        return None, None
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        return None, None
    close = bars["close"]
    if len(close) == 0:
        return None, None
    ret_1d = clean(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else None
    ret_1m = clean(close.iloc[-1] / close.iloc[-1 - _ONE_MONTH] - 1) if len(close) > _ONE_MONTH else None
    ret_3m = clean(close.iloc[-1] / close.iloc[-1 - _THREE_MONTH] - 1) if len(close) > _THREE_MONTH else None
    row = RotationRow(symbol=symbol, ret_1d=ret_1d, ret_1m=ret_1m, ret_3m=ret_3m)
    return row, close.index[-1]


def _rotation_block(
    store, symbol_map: dict[str, int], symbols: list[str], missing: list[str]
) -> tuple[list[RotationRow], pd.Timestamp | None]:
    rows: list[RotationRow] = []
    latest: pd.Timestamp | None = None
    for symbol in symbols:
        row, last_date = _rotation_row(store, symbol_map, symbol)
        if row is None:
            missing.append(symbol)
            continue
        rows.append(row)
        if last_date is not None and (latest is None or last_date > latest):
            latest = last_date
    # Rotation ranking: best 1-day movers first, symbols with no computable
    # ret_1d (too little history) sink to the bottom rather than sorting
    # arbitrarily.
    rows.sort(key=lambda r: (r.ret_1d is None, -(r.ret_1d if r.ret_1d is not None else 0.0)))
    return rows, latest


@router.get("/macro", response_model=MacroResponse)
def macro(request: Request) -> MacroResponse:
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
    sectors, sectors_latest = _rotation_block(store, symbol_map, SECTORS, missing)
    factors, factors_latest = _rotation_block(store, symbol_map, FACTORS, missing)
    if sectors_latest is not None:
        latest_dates.append(sectors_latest)
    if factors_latest is not None:
        latest_dates.append(factors_latest)

    as_of = iso(max(latest_dates)) if latest_dates else None

    return MacroResponse(
        yields=yields_block,
        net_liquidity=net_liquidity_block,
        sectors=sectors,
        factors=factors,
        as_of=as_of,
        missing=missing,
    )
