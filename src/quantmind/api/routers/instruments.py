"""instruments domain routes (Task A2): per-symbol metadata (name/exchange/
currency/secType/industry/region/provider — single-provenance law recorded
at sync) plus derived stats (52w high/low distance, annualized vol, beta vs
the app benchmark) and an OHLC candle window for InstrumentSheet's chart.

Reads only from the store/symbol map — never network, never a 500 (Global
Constraints). Unknown symbol -> 422 (pattern: routers/risk.py).
"""

from __future__ import annotations

import math

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from quantmind.risk.returns import InsufficientDataError, annualized_vol, rolling_beta, simple_returns

router = APIRouter()

_52W_TRADING_DAYS = 252
_BETA_WINDOW = 60
_MAX_CANDLES = 3650


def _clean(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf):
        return None
    return xf


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bars_for(request: Request, symbol: str) -> tuple[pd.DataFrame, int]:
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    if symbol not in symbol_map:
        raise HTTPException(422, detail=f"symbol {symbol!r} not in cache")
    con_id = symbol_map[symbol]
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    return bars, con_id


def _beta_vs_benchmark(store, close: pd.Series, symbol: str, benchmark: str) -> float | None:
    if symbol == benchmark:
        return 1.0
    symbol_map = store.read_symbol_map()
    bench_con_id = symbol_map.get(benchmark)
    if bench_con_id is None:
        return None
    try:
        bench_bars, _ = store.read_bars(con_id=bench_con_id, bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None
    aligned = pd.concat({"a": close, "b": bench_bars["close"]}, axis=1).dropna()
    window = min(_BETA_WINDOW, len(aligned) - 2)
    if window < 5:
        return None
    a_ret = simple_returns(aligned["a"])
    b_ret = simple_returns(aligned["b"])
    try:
        beta_series = rolling_beta(a_ret, b_ret, window=window)
    except InsufficientDataError:
        return None
    valid = beta_series.dropna()
    return _clean(valid.iloc[-1]) if len(valid) else None


class InstrumentResponse(BaseModel):
    symbol: str
    con_id: int
    long_name: str | None
    exchange: str | None
    currency: str | None
    sec_type: str | None
    industry: str | None
    region: str | None
    provider: str | None
    last_close: float | None
    high_52w: float | None
    low_52w: float | None
    pct_from_52w_high: float | None
    pct_from_52w_low: float | None
    ann_vol: float | None
    beta: float | None
    beta_benchmark: str
    as_of: str | None


class Candle(BaseModel):
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None


class CandlesResponse(BaseModel):
    symbol: str
    days: int
    candles: list[Candle]


@router.get("/instruments/{symbol}", response_model=InstrumentResponse)
def instrument(request: Request, symbol: str) -> InstrumentResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    bars, con_id = _bars_for(request, symbol)
    close = bars["close"]

    last = _clean(close.iloc[-1]) if len(close) else None
    window = close.iloc[-_52W_TRADING_DAYS:] if len(close) else close
    high_52w = _clean(window.max()) if len(window) else None
    low_52w = _clean(window.min()) if len(window) else None
    pct_from_high = (
        _clean(last / high_52w - 1.0) if last is not None and high_52w not in (None, 0) else None
    )
    pct_from_low = (
        _clean(last / low_52w - 1.0) if last is not None and low_52w not in (None, 0) else None
    )

    try:
        vol = _clean(annualized_vol(simple_returns(close)))
    except InsufficientDataError:
        vol = None

    beta = _beta_vs_benchmark(store, close, symbol, benchmark)

    meta = store.read_instrument_metadata(symbol) or {}

    return InstrumentResponse(
        symbol=symbol,
        con_id=con_id,
        long_name=meta.get("long_name"),
        exchange=meta.get("exchange"),
        currency=meta.get("currency"),
        sec_type=meta.get("sec_type"),
        industry=meta.get("industry"),
        region=meta.get("region"),
        provider=meta.get("provider"),
        last_close=last,
        high_52w=high_52w,
        low_52w=low_52w,
        pct_from_52w_high=pct_from_high,
        pct_from_52w_low=pct_from_low,
        ann_vol=vol,
        beta=beta,
        beta_benchmark=benchmark,
        as_of=_iso(close.index[-1]) if len(close) else None,
    )


@router.get("/instruments/{symbol}/candles", response_model=CandlesResponse)
def candles(
    request: Request,
    symbol: str,
    days: int = Query(180, ge=1, le=_MAX_CANDLES),
) -> CandlesResponse:
    bars, _ = _bars_for(request, symbol)
    window = bars.iloc[-days:]
    out = [
        Candle(
            date=_iso(idx),
            open=_clean(row["open"]),
            high=_clean(row["high"]),
            low=_clean(row["low"]),
            close=_clean(row["close"]),
            volume=_clean(row["volume"]),
        )
        for idx, row in window.iterrows()
    ]
    return CandlesResponse(symbol=symbol, days=days, candles=out)
