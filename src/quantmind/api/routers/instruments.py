"""instruments domain routes (Task A2): per-symbol metadata (name/exchange/
currency/secType/industry/region/provider — single-provenance law recorded
at sync) plus derived stats (52w high/low distance, annualized vol, beta vs
the app benchmark) and an OHLC candle window for InstrumentSheet's chart.

Reads only from the store/symbol map — never network, never a 500 (Global
Constraints). Unknown symbol -> 422 (pattern: routers/risk.py).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import (
    FxEvidenceOut,
    complete_fx_evidence,
    load_base_currency_series,
    read_instrument_metadata_map,
)
from quantmind.instruments.metadata import (
    UCITS_PROFILE_MAX_AGE_DAYS,
    MetadataProvenanceV1,
    ProfileFreshness,
    UcitsEtfProfileV1,
    is_ucits_profile_fresh,
)
from quantmind.risk.returns import (
    InsufficientDataError,
    annualized_vol,
    rolling_beta,
    simple_returns,
)

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


class InstrumentRiskReadiness(BaseModel):
    status: Literal["ready", "partial", "unavailable"]
    reason: Literal[
        "fx_unavailable", "missing_benchmark", "insufficient_history"
    ] | None
    benchmark: str
    base_currency: str
    fx: FxEvidenceOut
    note: str


def _has_price_history(store, symbol_map: dict[str, int], symbol: str) -> bool:
    con_id = symbol_map.get(symbol)
    if con_id is None:
        return False
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return False
    return "close" in bars and not bars["close"].dropna().empty


def _risk_state(
    *,
    status: Literal["ready", "partial", "unavailable"],
    reason: Literal[
        "fx_unavailable", "missing_benchmark", "insufficient_history"
    ] | None,
    benchmark: str,
    base_currency: str,
    fx: FxEvidenceOut,
    note: str,
) -> InstrumentRiskReadiness:
    return InstrumentRiskReadiness(
        status=status,
        reason=reason,
        benchmark=benchmark,
        base_currency=base_currency,
        fx=fx,
        note=note,
    )


def _base_currency_risk_stats(
    request: Request, symbol: str, benchmark: str
) -> tuple[float | None, float | None, InstrumentRiskReadiness]:
    """Return independently available stats plus discriminated evidence."""
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    base_currency = getattr(request.app.state, "base_currency", "USD")
    benchmark_available = _has_price_history(store, symbol_map, benchmark)

    # Volatility depends only on the instrument. Resolve that evidence first so
    # an unavailable foreign benchmark cannot suppress a metric we can compute
    # honestly from the instrument's own base-normalized history.
    try:
        asset_series, _asset_currencies, asset_converter = load_base_currency_series(
            store,
            symbol_map,
            [symbol],
            years=0,
            base_currency=base_currency,
        )
    except (HTTPException, KeyError) as exc:
        metadata = read_instrument_metadata_map(store)
        missing_currencies = sorted(
            {
                str((metadata.get(item) or {}).get("currency") or "UNKNOWN")
                .strip()
                .upper()
                for item in [symbol]
                if str((metadata.get(item) or {}).get("currency") or "UNKNOWN")
                .strip()
                .upper()
                != base_currency
            }
        )
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        fx = FxEvidenceOut(
            status="incomplete",
            base_currency=base_currency,
            source=None,
            as_of=None,
            fetched_at=None,
            missing_currencies=missing_currencies or ["UNKNOWN"],
            note=f"Dated FX normalization is unavailable: {detail}",
        )
        return None, None, _risk_state(
            status="unavailable",
            reason="fx_unavailable",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=fx,
            note="Risk metrics are unavailable because dated FX evidence is missing.",
        )

    asset_fx = complete_fx_evidence(asset_converter, base_currency=base_currency)
    asset = asset_series[symbol]
    try:
        vol = _clean(annualized_vol(simple_returns(asset)))
    except InsufficientDataError:
        vol = None

    if not benchmark_available:
        return vol, None, _risk_state(
            status="partial" if vol is not None else "unavailable",
            reason="missing_benchmark",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=asset_fx,
            note=(
                f"Benchmark {benchmark} is not cached; volatility uses "
                f"{base_currency}-normalized history, but beta is unavailable."
            ),
        )

    try:
        series, _currencies, converter = load_base_currency_series(
            store,
            symbol_map,
            [symbol, benchmark],
            years=0,
            base_currency=base_currency,
        )
    except (HTTPException, KeyError) as exc:
        metadata = read_instrument_metadata_map(store)
        benchmark_currency = (
            str((metadata.get(benchmark) or {}).get("currency") or "UNKNOWN")
            .strip()
            .upper()
        )
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        missing = (
            [benchmark_currency]
            if benchmark_currency != base_currency
            else ["UNKNOWN"]
        )
        fx = FxEvidenceOut(
            status="incomplete",
            base_currency=base_currency,
            source=asset_fx.source,
            as_of=asset_fx.as_of,
            fetched_at=asset_fx.fetched_at,
            missing_currencies=missing,
            note=f"Benchmark FX normalization is unavailable: {detail}",
        )
        return vol, None, _risk_state(
            status="partial" if vol is not None else "unavailable",
            reason="fx_unavailable",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=fx,
            note=(
                f"Volatility uses {base_currency}-normalized instrument history, "
                f"but beta vs {benchmark} is unavailable because benchmark FX is missing."
            ),
        )

    fx = complete_fx_evidence(converter, base_currency=base_currency)
    asset = series[symbol]
    benchmark_close = series[benchmark]
    aligned = pd.concat({"a": asset, "b": benchmark_close}, axis=1).dropna()
    window = min(_BETA_WINDOW, len(aligned) - 2)
    if window < 5:
        return vol, None, _risk_state(
            status="partial" if vol is not None else "unavailable",
            reason="insufficient_history",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=fx,
            note=f"Insufficient overlapping history for beta vs {benchmark}.",
        )
    a_ret = simple_returns(aligned["a"])
    b_ret = simple_returns(aligned["b"])
    try:
        beta_series = rolling_beta(a_ret, b_ret, window=window)
    except InsufficientDataError:
        return vol, None, _risk_state(
            status="partial" if vol is not None else "unavailable",
            reason="insufficient_history",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=fx,
            note=f"Insufficient overlapping history for beta vs {benchmark}.",
        )
    valid = beta_series.dropna()
    beta = _clean(valid.iloc[-1]) if len(valid) else None
    if beta is None or vol is None:
        return vol, beta, _risk_state(
            status="partial" if vol is not None or beta is not None else "unavailable",
            reason="insufficient_history",
            benchmark=benchmark,
            base_currency=base_currency,
            fx=fx,
            note=f"Insufficient usable history for complete risk metrics vs {benchmark}.",
        )
    return vol, beta, _risk_state(
        status="ready",
        reason=None,
        benchmark=benchmark,
        base_currency=base_currency,
        fx=fx,
        note=f"Volatility and beta are ready from {base_currency}-normalized history.",
    )


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
    isin: str | None
    primary_exchange: str | None
    local_symbol: str | None
    trading_class: str | None
    stock_type: str | None
    valid_exchanges: list[str]
    issuer_id: str | None
    ucits_profile_status: ProfileFreshness | None
    ucits_profile_reason: str | None
    ucits_profile_last_successful_provenance: MetadataProvenanceV1 | None
    ucits_profile: UcitsEtfProfileV1 | None
    last_close: float | None
    high_52w: float | None
    low_52w: float | None
    pct_from_52w_high: float | None
    pct_from_52w_low: float | None
    ann_vol: float | None
    beta: float | None
    beta_benchmark: str
    risk_base_currency: str
    risk_fx_source: str | None
    risk_fx_as_of: str | None
    risk: InstrumentRiskReadiness
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
    meta = read_instrument_metadata_map(store).get(symbol) or {}
    if meta and meta.get("con_id") != con_id:
        raise HTTPException(
            422,
            detail=(
                f"instrument metadata contract identity for {symbol!r} "
                "does not match the current symbol map; run sync"
            ),
        )
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

    vol, beta, risk = _base_currency_risk_stats(request, symbol, benchmark)

    profile = None
    profile_status = meta.get("ucits_profile_status")
    profile_reason = meta.get("ucits_profile_reason")
    profile_isin = meta.get("ucits_profile_isin")
    raw_profile_provenance = meta.get("ucits_profile_last_successful_provenance")
    profile_last_successful_provenance = (
        MetadataProvenanceV1.model_validate(raw_profile_provenance, strict=False)
        if raw_profile_provenance is not None
        else None
    )
    if profile_status == ProfileFreshness.FRESH.value and profile_isin:
        try:
            profile = store.read_ucits_profile(profile_isin)
        except ValueError:
            profile_status = ProfileFreshness.MISSING.value
            profile_reason = "cached UCITS profile is corrupt; run sync"
            profile_last_successful_provenance = None
        if profile is None and profile_status == ProfileFreshness.FRESH.value:
            profile_status = ProfileFreshness.MISSING.value
            profile_reason = "cached UCITS profile is missing; run sync"
            profile_last_successful_provenance = None
        elif profile is not None and not is_ucits_profile_fresh(
            profile, now=datetime.now(UTC)
        ):
            profile_last_successful_provenance = profile.provenance
            profile = None
            profile_status = ProfileFreshness.STALE.value
            profile_reason = (
                f"cached UCITS profile exceeds the {UCITS_PROFILE_MAX_AGE_DAYS:g}-day "
                "freshness window; run sync"
            )
        elif profile is not None:
            profile_last_successful_provenance = profile.provenance
    elif (
        profile_status == ProfileFreshness.STALE.value
        and profile_isin
        and profile_last_successful_provenance is None
    ):
        try:
            stale_profile = store.read_ucits_profile(profile_isin)
        except ValueError:
            stale_profile = None
        if stale_profile is not None:
            profile_last_successful_provenance = stale_profile.provenance
    if profile_status not in {
        ProfileFreshness.FRESH.value,
        ProfileFreshness.STALE.value,
    }:
        profile_last_successful_provenance = None

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
        isin=meta.get("isin"),
        primary_exchange=meta.get("primary_exchange"),
        local_symbol=meta.get("local_symbol"),
        trading_class=meta.get("trading_class"),
        stock_type=meta.get("stock_type"),
        valid_exchanges=list(meta.get("valid_exchanges") or []),
        issuer_id=meta.get("issuer_id"),
        ucits_profile_status=profile_status,
        ucits_profile_reason=profile_reason,
        ucits_profile_last_successful_provenance=profile_last_successful_provenance,
        ucits_profile=profile,
        last_close=last,
        high_52w=high_52w,
        low_52w=low_52w,
        pct_from_52w_high=pct_from_high,
        pct_from_52w_low=pct_from_low,
        ann_vol=vol,
        beta=beta,
        beta_benchmark=benchmark,
        risk_base_currency=risk.base_currency,
        risk_fx_source=risk.fx.source,
        risk_fx_as_of=risk.fx.as_of,
        risk=risk,
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
