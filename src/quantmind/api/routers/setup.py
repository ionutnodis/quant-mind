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
    book: BookReadiness
    next_action: Literal[
        "configure_account",
        "start_gateway",
        "wait_for_gateway",
        "sync_market_data",
        "sync_option_data",
        "pin_book",
        "resolve_currency",
        "resolve_instruments",
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
    required_symbols = list(dict.fromkeys([benchmark, *required_symbols]))
    if not symbol_map:
        return MarketDataReadiness(
            status="empty",
            symbols=len(required_symbols),
            ready_symbols=0,
            missing_symbols=required_symbols,
            stale_symbols=[],
            corrupt_symbols=[],
            series=len(store.list_series()),
            as_of=None,
            age_days=None,
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
        watermarks.append(watermark)
        age = _business_age_days(watermark.date(), today)
        if age > 3:
            stale_symbols.append(symbol)
        else:
            ready_symbols.append(symbol)

    weakest = min(watermarks).date() if watermarks else None
    age_days = None if weakest is None else _business_age_days(weakest, today)
    if missing_symbols or corrupt_symbols:
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


def _book_status(store, state) -> BookReadiness:
    snapshots = list_books(store)

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

    unsupported_currencies = sorted(
        {
            position.currency or "UNKNOWN"
            for position in latest.positions
            if position.currency != latest.base_currency
        }
    )
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
    elif unsupported_currencies:
        reason = "unsupported_currency"
    elif unsupported_security_types:
        reason = "unsupported_security_type"
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
            if reason in {"unsupported_currency", "unsupported_security_type"}
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


@router.get("/setup/status", response_model=SetupStatus)
def get_setup_status(request: Request) -> SetupStatus:
    state = request.app.state
    broker_status = state.broker_connection_status
    market_data = _market_data_status(state.store, request.app.state.benchmark)
    macro_data = _macro_data_status(state.store)
    book = _book_status(state.store, state)
    options_data = _options_data_status(state.store)

    if state.broker_connection_error == "account_selection_required":
        next_action = "configure_account"
    elif broker_status == "unavailable":
        next_action = "start_gateway"
    elif broker_status == "connecting":
        next_action = "wait_for_gateway"
    elif market_data.status != "ready" or macro_data.status != "ready":
        next_action = "sync_market_data"
    elif book.reason == "unsupported_security_type":
        next_action = "resolve_instruments"
    elif book.status == "unsupported":
        next_action = "resolve_currency"
    elif book.status != "ready":
        next_action = "pin_book"
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
        book=book,
        next_action=next_action,
    )
