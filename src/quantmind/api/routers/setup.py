"""Read-only first-run diagnostics for the guided setup screen.

This route deliberately observes app-owned broker connection state rather
than making a broker request. Opening Setup must never create IBKR traffic or
turn a slow Gateway into a slow health check.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Literal

import numpy as np
from fastapi import APIRouter, Request
from pydantic import BaseModel

from quantmind.api.routers.book import _account_fingerprint, list_books
from quantmind.datastore.options_store import OptionsStore
from quantmind.datastore.store import PORTFOLIO_DISCOVERY_FAILURE_SYMBOL
from quantmind.fx import FxConversionUnavailable, FxConverter, FxObservationStale
from quantmind.instruments.metadata import (
    ProfileFreshness,
    is_potential_ucits_isin,
    is_ucits_profile_fresh,
)

router = APIRouter()


def _business_age_days(observation, today) -> int:
    """Weekday-aware age for exchange data (holiday calendars come later)."""
    if observation >= today:
        return 0
    return int(np.busday_count(observation.isoformat(), today.isoformat()))


class ApiReadiness(BaseModel):
    status: Literal["ready"]
    version: str


class BrokerReadiness(BaseModel):
    status: Literal["connecting", "connected", "unavailable"]
    provider: Literal["IBKR"] = "IBKR"
    mode: Literal["paper", "live", "custom"] | None
    error: str | None


class MarketDataReadiness(BaseModel):
    status: Literal["empty", "incomplete", "stale", "ready"]
    symbols: int
    ready_symbols: int
    missing_symbols: list[str]
    stale_symbols: list[str]
    corrupt_symbols: list[str]
    series: int
    as_of: str | None
    age_days: int | None
    portfolio_discovery_error: Literal["live_portfolio_unavailable"] | None = None


class MacroDataReadiness(BaseModel):
    status: Literal["empty", "incomplete", "stale", "ready"]
    required_series: int
    ready_series: int
    missing_series: list[str]
    stale_series: list[str]
    corrupt_series: list[str]
    as_of: str | None
    age_days: int | None


class OptionsDataReadiness(BaseModel):
    status: Literal["not_required", "missing", "partial", "stale", "ready"]
    total_positions: int
    priced_positions: int
    missing_contracts: list[str]
    stale_chains: list[str]
    chain_as_of: str | None
    chain_age_days: int | None


class FxDataReadiness(BaseModel):
    status: Literal["not_required", "missing", "stale", "ready"]
    base_currency: str
    required_currencies: list[str]
    missing_currencies: list[str]
    provider: str | None
    as_of: str | None


class UcitsDataReadiness(BaseModel):
    status: Literal["not_required", "incomplete", "stale", "ready"]
    total_etfs: int
    ready_profiles: int
    missing_symbols: list[str]
    stale_symbols: list[str]


class BookReadiness(BaseModel):
    status: Literal["not_pinned", "stale", "unsupported", "ready"]
    snapshot_count: int
    latest_snapshot_id: str | None
    valuation_ts: str | None
    option_positions: int
    age_days: int | None
    source: Literal["live_ibkr", "manual", "legacy"] | None
    account_fingerprint: str | None
    broker_mode: Literal["paper", "live", "custom"] | None
    unsupported_currencies: list[str]
    unsupported_security_types: list[str]
    reason: Literal[
        "empty_book",
        "stale_snapshot",
        "invalid_timestamp",
        "legacy_scope",
        "base_currency_mismatch",
        "cross_currency_option",
        "account_mismatch",
        "mode_mismatch",
        "unsupported_currency",
        "unsupported_security_type",
    ] | None


class SetupStatus(BaseModel):
    overall: Literal["needs_attention", "ready"]
    api: ApiReadiness
    broker: BrokerReadiness
    market_data: MarketDataReadiness
    macro_data: MacroDataReadiness
    options_data: OptionsDataReadiness
    fx_data: FxDataReadiness
    ucits_data: UcitsDataReadiness
    book: BookReadiness
    next_action: Literal[
        "configure_account",
        "start_gateway",
        "wait_for_gateway",
        "sync_market_data",
        "sync_option_data",
        "sync_fx_data",
        "pin_book",
        "resolve_currency",
        "resolve_instruments",
        "resolve_option_currency",
        "ready",
    ]


def _market_data_status(store, benchmark: str) -> MarketDataReadiness:
    try:
        symbol_map = store.read_symbol_map()
        required_symbols = store.read_required_symbols()
    except Exception:
        return MarketDataReadiness(
            status="incomplete",
            symbols=0,
            ready_symbols=0,
            missing_symbols=[],
            stale_symbols=[],
            corrupt_symbols=["symbols.json"],
            series=len(store.list_series()),
            as_of=None,
            age_days=None,
        )

    has_required_manifest = bool(required_symbols)
    portfolio_discovery_failed = (
        PORTFOLIO_DISCOVERY_FAILURE_SYMBOL in required_symbols
    )
    required_symbols = [
        symbol
        for symbol in required_symbols
        if symbol != PORTFOLIO_DISCOVERY_FAILURE_SYMBOL
    ]
    try:
        instrument_metadata = store.read_all_instrument_metadata()
    except Exception:
        instrument_metadata = {}
    required_symbols = list(dict.fromkeys([benchmark, *required_symbols]))
    if not symbol_map:
        return MarketDataReadiness(
            status="incomplete" if portfolio_discovery_failed else "empty",
            symbols=len(required_symbols),
            ready_symbols=0,
            missing_symbols=required_symbols,
            stale_symbols=[],
            corrupt_symbols=[],
            series=len(store.list_series()),
            as_of=None,
            age_days=None,
            portfolio_discovery_error=(
                "live_portfolio_unavailable" if portfolio_discovery_failed else None
            ),
        )

    today = datetime.now(timezone.utc).date()
    watermarks = []
    ready_symbols: list[str] = []
    missing_symbols: list[str] = []
    stale_symbols: list[str] = []
    corrupt_symbols: list[str] = []
    # Legacy stores predate required_symbols.json; retain their current
    # behavior until the next sync writes an explicit current universe.
    universe = required_symbols if has_required_manifest else list(symbol_map)
    if benchmark not in universe:
        universe.insert(0, benchmark)
    for symbol in sorted(universe):
        con_id = symbol_map.get(symbol)
        if con_id is None:
            missing_symbols.append(symbol)
            continue
        try:
            watermark = store.watermark(con_id, "1d")
        except Exception:
            corrupt_symbols.append(symbol)
            continue
        if watermark is None:
            missing_symbols.append(symbol)
            continue
        if has_required_manifest:
            metadata = instrument_metadata.get(symbol)
            currency = str((metadata or {}).get("currency") or "").strip().upper()
            if not metadata or not currency:
                missing_symbols.append(symbol)
                continue
            if (
                metadata.get("con_id") != con_id
                or len(currency) != 3
                or not currency.isalpha()
            ):
                corrupt_symbols.append(symbol)
                continue
        watermarks.append(watermark)
        age = _business_age_days(watermark.date(), today)
        if age > 3:
            stale_symbols.append(symbol)
        else:
            ready_symbols.append(symbol)

    weakest = min(watermarks).date() if watermarks else None
    age_days = None if weakest is None else _business_age_days(weakest, today)
    if portfolio_discovery_failed or missing_symbols or corrupt_symbols:
        status = "incomplete"
    elif stale_symbols:
        status = "stale"
    else:
        status = "ready"
    return MarketDataReadiness(
        status=status,
        symbols=len(universe),
        ready_symbols=len(ready_symbols),
        missing_symbols=missing_symbols,
        stale_symbols=stale_symbols,
        corrupt_symbols=corrupt_symbols,
        series=len(store.list_series()),
        as_of=None if weakest is None else weakest.isoformat(),
        age_days=age_days,
        portfolio_discovery_error=(
            "live_portfolio_unavailable" if portfolio_discovery_failed else None
        ),
    )


_MACRO_MAX_AGE_DAYS = {
    "NET_LIQUIDITY": 10,  # weekly source cadence
    "US10Y": 5,
    "US2Y": 5,
    "US3M": 5,
}


def _macro_data_status(store) -> MacroDataReadiness:
    today = datetime.now(timezone.utc).date()
    watermarks = []
    ready_series: list[str] = []
    missing_series: list[str] = []
    stale_series: list[str] = []
    corrupt_series: list[str] = []
    for name, max_age in _MACRO_MAX_AGE_DAYS.items():
        try:
            watermark = store.series_watermark(name)
        except Exception:
            corrupt_series.append(name)
            continue
        if watermark is None:
            missing_series.append(name)
            continue
        watermarks.append(watermark)
        age = max(0, (today - watermark.date()).days)
        if age > max_age:
            stale_series.append(name)
        else:
            ready_series.append(name)

    weakest = min(watermarks).date() if watermarks else None
    age_days = None if weakest is None else max(0, (today - weakest).days)
    if len(missing_series) == len(_MACRO_MAX_AGE_DAYS):
        status = "empty"
    elif missing_series or corrupt_series:
        status = "incomplete"
    elif stale_series:
        status = "stale"
    else:
        status = "ready"
    return MacroDataReadiness(
        status=status,
        required_series=len(_MACRO_MAX_AGE_DAYS),
        ready_series=len(ready_series),
        missing_series=missing_series,
        stale_series=stale_series,
        corrupt_series=corrupt_series,
        as_of=None if weakest is None else weakest.isoformat(),
        age_days=age_days,
    )


def _book_status(store, state, *, snapshots=None) -> BookReadiness:
    snapshots = list_books(store) if snapshots is None else snapshots

    if not snapshots:
        return BookReadiness(
            status="not_pinned",
            snapshot_count=0,
            latest_snapshot_id=None,
            valuation_ts=None,
            option_positions=0,
            age_days=None,
            source=None,
            account_fingerprint=None,
            broker_mode=None,
            unsupported_currencies=[],
            unsupported_security_types=[],
            reason=None,
        )

    latest = max(snapshots, key=lambda snapshot: snapshot.valuation_ts)
    try:
        valuation_date = datetime.fromisoformat(
            latest.valuation_ts.replace("Z", "+00:00")
        ).date()
        age_days = max(0, (datetime.now(timezone.utc).date() - valuation_date).days)
    except ValueError:
        age_days = None

    unknown_currencies = sorted(
        {
            position.currency or "UNKNOWN"
            for position in latest.positions
            if not position.currency or position.currency == "UNKNOWN"
        }
    )
    cross_currency_options = sorted(
        {
            position.currency
            for position in latest.positions
            if position.sec_type == "OPT"
            and position.currency
            and position.currency != "UNKNOWN"
            and position.currency != getattr(state, "base_currency", "USD")
        }
    )
    unsupported_currencies = sorted({*unknown_currencies, *cross_currency_options})
    unsupported_security_types = sorted(
        {
            position.sec_type
            for position in latest.positions
            if position.sec_type not in {"STK", "OPT"}
        }
    )
    reason = None
    if not latest.positions:
        reason = "empty_book"
    elif unknown_currencies:
        reason = "unsupported_currency"
    elif cross_currency_options:
        reason = "cross_currency_option"
    elif unsupported_security_types:
        reason = "unsupported_security_type"
    elif latest.base_currency != getattr(state, "base_currency", "USD"):
        reason = "base_currency_mismatch"
    elif age_days is None:
        reason = "invalid_timestamp"
    elif age_days > 0:
        reason = "stale_snapshot"
    elif latest.source == "legacy":
        reason = "legacy_scope"
    elif latest.source == "live_ibkr":
        current_fingerprint = _account_fingerprint(
            getattr(state, "broker_account_id", None)
        )
        if (
            latest.account_fingerprint is None
            or current_fingerprint is None
            or latest.account_fingerprint != current_fingerprint
        ):
            reason = "account_mismatch"
        elif latest.broker_mode != getattr(state, "broker_mode", None):
            reason = "mode_mismatch"

    return BookReadiness(
        status=(
            "ready"
            if reason is None
            else "unsupported"
            if reason
            in {
                "unsupported_currency",
                "unsupported_security_type",
                "cross_currency_option",
            }
            else "stale"
        ),
        snapshot_count=len(snapshots),
        latest_snapshot_id=latest.snapshot_id,
        valuation_ts=latest.valuation_ts,
        option_positions=sum(
            1 for position in latest.positions if position.sec_type == "OPT"
        ),
        age_days=age_days,
        source=latest.source,
        account_fingerprint=latest.account_fingerprint,
        broker_mode=latest.broker_mode,
        unsupported_currencies=unsupported_currencies,
        unsupported_security_types=unsupported_security_types,
        reason=reason,
    )


def _options_data_status(store) -> OptionsDataReadiness:
    snapshots = list_books(store)
    if not snapshots:
        return OptionsDataReadiness(
            status="not_required",
            total_positions=0,
            priced_positions=0,
            missing_contracts=[],
            stale_chains=[],
            chain_as_of=None,
            chain_age_days=None,
        )

    latest = max(snapshots, key=lambda snapshot: snapshot.valuation_ts)
    positions = [position for position in latest.positions if position.sec_type == "OPT"]
    if not positions:
        return OptionsDataReadiness(
            status="not_required",
            total_positions=0,
            priced_positions=0,
            missing_contracts=[],
            stale_chains=[],
            chain_as_of=None,
            chain_age_days=None,
        )

    options_store = OptionsStore(store.root)
    chains: dict[str, tuple[object, object] | None] = {}
    missing_contracts: list[str] = []
    stale_chains: set[str] = set()
    chain_dates = []
    priced_positions = 0
    today = datetime.now(timezone.utc).date()

    for position in positions:
        contract_label = (
            f"{position.symbol} {position.expiry or '?'} "
            f"{position.strike:g} {position.right or '?'}"
            if position.strike is not None
            else f"{position.symbol} {position.expiry or '?'} ? {position.right or '?'}"
        )
        if position.expiry is None or position.strike is None or position.right is None:
            missing_contracts.append(contract_label)
            continue
        if position.symbol not in chains:
            try:
                chains[position.symbol] = options_store.read_chain(position.symbol)
            except Exception:
                chains[position.symbol] = None
        cached = chains[position.symbol]
        if cached is None:
            missing_contracts.append(contract_label)
            continue
        frame, meta = cached
        try:
            chain_date = datetime.fromisoformat(meta.as_of.replace("Z", "+00:00")).date()
            chain_dates.append(chain_date)
            if _business_age_days(chain_date, today) > 3:
                stale_chains.add(position.symbol)
            matches = frame[
                (frame["expiry"].astype(str).str.replace("-", "") == position.expiry.replace("-", ""))
                & ((frame["strike"].astype(float) - position.strike).abs() < 1e-8)
                & (frame["right"].astype(str) == position.right)
            ]
            if latest.source == "live_ibkr" and position.con_id is not None:
                contract_ids = np.asarray(
                    frame.loc[matches.index, "con_id"], dtype=float
                )
                matches = matches.loc[contract_ids == position.con_id]
            else:
                multipliers = np.asarray(matches["multiplier"], dtype=float)
                matches = matches.loc[
                    np.isclose(multipliers, position.multiplier, atol=1e-8)
                ]
            if len(matches) != 1:
                missing_contracts.append(contract_label)
                continue
            row = matches.iloc[0]

            def usable_quote(value) -> float | None:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    return None
                return parsed if math.isfinite(parsed) and parsed >= 0 else None

            bid = usable_quote(row["bid"])
            ask = usable_quote(row["ask"])
            iv = float(row["iv"])
            mark_available = (bid is not None or ask is not None) and not (
                bid is not None and ask is not None and ask < bid
            )
            if not mark_available or not math.isfinite(iv) or iv <= 0:
                missing_contracts.append(contract_label)
                continue
        except (KeyError, TypeError, ValueError):
            missing_contracts.append(contract_label)
            continue
        priced_positions += 1

    total_positions = len(positions)
    if priced_positions == 0:
        status = "missing"
    elif priced_positions < total_positions:
        status = "partial"
    elif stale_chains:
        status = "stale"
    else:
        status = "ready"
    weakest = min(chain_dates) if chain_dates else None
    return OptionsDataReadiness(
        status=status,
        total_positions=total_positions,
        priced_positions=priced_positions,
        missing_contracts=missing_contracts,
        stale_chains=sorted(stale_chains),
        chain_as_of=None if weakest is None else weakest.isoformat(),
        chain_age_days=None if weakest is None else _business_age_days(weakest, today),
    )


def _fx_data_status(
    store,
    default_base_currency: str = "USD",
    benchmark: str = "SPY",
    *,
    snapshots=None,
) -> FxDataReadiness:
    snapshots = list_books(store) if snapshots is None else snapshots
    if not snapshots:
        return FxDataReadiness(
            status="not_required",
            base_currency=default_base_currency,
            required_currencies=[],
            missing_currencies=[],
            provider=None,
            as_of=None,
        )
    latest = max(snapshots, key=lambda snapshot: snapshot.valuation_ts)
    if latest.base_currency != default_base_currency:
        return FxDataReadiness(
            status="missing",
            base_currency=default_base_currency,
            required_currencies=[],
            missing_currencies=[],
            provider=None,
            as_of=None,
        )
    try:
        benchmark_metadata = store.read_instrument_metadata(benchmark) or {}
    except Exception:
        benchmark_metadata = {}
    benchmark_currency = str(benchmark_metadata.get("currency") or "").strip().upper()
    if len(benchmark_currency) != 3 or not benchmark_currency.isalpha():
        return FxDataReadiness(
            status="missing",
            base_currency=latest.base_currency,
            required_currencies=[],
            missing_currencies=[],
            provider=None,
            as_of=None,
        )
    required = sorted(
        {
            position.currency
            for position in latest.positions
            if position.currency
            and position.currency != "UNKNOWN"
            and position.currency != latest.base_currency
        }
        | (
            {benchmark_currency}
            if benchmark_currency != latest.base_currency
            else set()
        )
    )
    if not required:
        return FxDataReadiness(
            status="not_required",
            base_currency=latest.base_currency,
            required_currencies=[],
            missing_currencies=[],
            provider=None,
            as_of=None,
        )
    try:
        converter = FxConverter.from_store(
            store,
            base_currency=latest.base_currency,
            currencies=set(required),
        )
    except (FxConversionUnavailable, ValueError):
        return FxDataReadiness(
            status="missing",
            base_currency=latest.base_currency,
            required_currencies=required,
            missing_currencies=required,
            provider=None,
            as_of=None,
        )

    today = datetime.now(timezone.utc).date()
    missing: list[str] = []
    stale: list[str] = []
    for currency in required:
        try:
            converter.rate(currency, today)
        except FxObservationStale:
            stale.append(currency)
        except FxConversionUnavailable:
            missing.append(currency)
    return FxDataReadiness(
        status="stale" if stale else "missing" if missing else "ready",
        base_currency=latest.base_currency,
        required_currencies=required,
        missing_currencies=sorted({*missing, *stale}),
        provider=converter.source,
        as_of=converter.as_of,
    )


def _ucits_data_status(store) -> UcitsDataReadiness:
    try:
        metadata = store.read_all_instrument_metadata()
    except (OSError, TypeError, ValueError):
        return UcitsDataReadiness(
            status="incomplete",
            total_etfs=0,
            ready_profiles=0,
            missing_symbols=["INSTRUMENT_METADATA"],
            stale_symbols=[],
        )
    etfs = {
        symbol: fields
        for symbol, fields in metadata.items()
        if str(fields.get("stock_type") or "").strip().upper() == "ETF"
        and is_potential_ucits_isin(fields.get("isin"))
    }
    if not etfs:
        return UcitsDataReadiness(
            status="not_required",
            total_etfs=0,
            ready_profiles=0,
            missing_symbols=[],
            stale_symbols=[],
        )
    ready = 0
    missing: list[str] = []
    stale: list[str] = []
    for symbol, fields in etfs.items():
        status = fields.get("ucits_profile_status")
        if status == ProfileFreshness.FRESH.value:
            try:
                profile = store.read_ucits_profile(fields.get("ucits_profile_isin") or "")
            except (TypeError, ValueError):
                profile = None
            if profile is not None and is_ucits_profile_fresh(
                profile, now=datetime.now(timezone.utc)
            ):
                ready += 1
            elif profile is not None:
                stale.append(symbol)
            else:
                missing.append(symbol)
        elif status == ProfileFreshness.STALE.value:
            stale.append(symbol)
        else:
            missing.append(symbol)
    return UcitsDataReadiness(
        status=(
            "incomplete"
            if missing
            else "stale"
            if stale
            else "ready"
        ),
        total_etfs=len(etfs),
        ready_profiles=ready,
        missing_symbols=sorted(missing),
        stale_symbols=sorted(stale),
    )


@router.get("/setup/status", response_model=SetupStatus)
def get_setup_status(request: Request) -> SetupStatus:
    state = request.app.state
    snapshots = list_books(state.store)
    broker_status = state.broker_connection_status
    market_data = _market_data_status(state.store, request.app.state.benchmark)
    macro_data = _macro_data_status(state.store)
    book = _book_status(state.store, state, snapshots=snapshots)
    options_data = _options_data_status(state.store)
    fx_data = _fx_data_status(
        state.store,
        getattr(state, "base_currency", "USD"),
        state.benchmark,
        snapshots=snapshots,
    )
    ucits_data = _ucits_data_status(state.store)

    if state.broker_connection_error == "account_selection_required":
        next_action = "configure_account"
    elif broker_status == "unavailable":
        next_action = "start_gateway"
    elif broker_status == "connecting":
        next_action = "wait_for_gateway"
    elif market_data.status != "ready" or macro_data.status != "ready":
        next_action = "sync_market_data"
    elif book.reason == "cross_currency_option":
        next_action = "resolve_option_currency"
    elif book.reason == "unsupported_security_type":
        next_action = "resolve_instruments"
    elif book.status == "unsupported":
        next_action = "resolve_currency"
    elif book.status != "ready":
        next_action = "pin_book"
    elif fx_data.status == "missing" and not fx_data.required_currencies:
        next_action = "sync_market_data"
    elif fx_data.status not in {"not_required", "ready"}:
        next_action = "sync_fx_data"
    elif options_data.status not in {"not_required", "ready"}:
        next_action = "sync_option_data"
    else:
        next_action = "ready"

    return SetupStatus(
        overall="ready" if next_action == "ready" else "needs_attention",
        api=ApiReadiness(status="ready", version=request.app.version),
        broker=BrokerReadiness(
            status=broker_status,
            mode=state.broker_mode,
            error=state.broker_connection_error,
        ),
        market_data=market_data,
        macro_data=macro_data,
        options_data=options_data,
        fx_data=fx_data,
        ucits_data=ucits_data,
        book=book,
        next_action=next_action,
    )
