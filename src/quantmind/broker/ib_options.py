"""Option chain parameters + paced snapshot fetch (Task A3).

Split into pure selection helpers (no I/O — unit tested directly) and thin
async wrappers over `ib` (an ib_async `IB` instance, or any fake exposing the
same three async methods: `reqSecDefOptParamsAsync`, `qualifyContractsAsync`,
`reqTickersAsync` — pattern: broker/ib_broker.py + tests/test_sync.py's
FakeBroker). The network-touching paths are exercised only via fakes here;
live behaviour is covered by the opt-in E2E smoke test (Engineering Constraint
1's discipline extended to options).

Chain ingestion policy (wave-3 plan Task A3): monthlies only, expiring within
`max_days` of `as_of`, strikes within `±pct` of spot — this keeps the paced
OPRA snapshot fetch to a bounded, liquid slice of the chain rather than every
listed strike/expiry.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence


@dataclass(frozen=True)
class OptionChainParams:
    underlying_symbol: str
    underlying_con_id: int
    trading_class: str
    exchange: str
    multiplier: str
    expirations: tuple[str, ...]  # "YYYYMMDD", sorted
    strikes: tuple[float, ...]  # sorted
    currency: str | None = None


@dataclass(frozen=True)
class OptionQuote:
    underlier: str
    expiry: str  # "YYYYMMDD"
    strike: float
    right: str  # "C" | "P"
    con_id: int | None
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    multiplier: float
    observed_at: str
    market_data_type: int
    received_at: str


# --- pure selection helpers ---


def _is_monthly_expiry(expiry: str) -> bool:
    """Standard US equity-option monthlies expire the third Friday of the
    month; that date always falls in [15, 21]."""
    d = datetime.strptime(expiry, "%Y%m%d").date()
    return d.weekday() == 4 and 15 <= d.day <= 21


def select_monthly_expiries(expirations: Sequence[str], as_of: date, max_days: int = 90) -> list[str]:
    """Monthly expiries strictly in the future (or today), within `max_days`."""
    out = []
    for e in expirations:
        d = datetime.strptime(e, "%Y%m%d").date()
        days = (d - as_of).days
        if 0 <= days <= max_days and _is_monthly_expiry(e):
            out.append(e)
    return sorted(out)


def select_strikes_near_spot(strikes: Sequence[float], spot: float, pct: float = 0.15) -> list[float]:
    """Strikes within ±`pct` of `spot` (inclusive)."""
    lo, hi = spot * (1 - pct), spot * (1 + pct)
    return sorted(s for s in strikes if lo <= s <= hi)


# --- I/O: chain params ---


async def fetch_chain_params(ib, symbol: str, con_id: int, sec_type: str = "STK") -> OptionChainParams:
    """reqSecDefOptParams for `symbol`; prefers the SMART-routed chain (the
    liquid, consolidated one for US equities/ETFs) and falls back to the
    first chain returned when SMART isn't present."""
    chains = await ib.reqSecDefOptParamsAsync(symbol, "", sec_type, con_id)
    if not chains:
        raise LookupError(f"no option chain parameters returned for {symbol!r}")
    returned_con_ids = [getattr(chain, "underlyingConId", None) for chain in chains]
    if any(
        not isinstance(returned_con_id, int)
        or isinstance(returned_con_id, bool)
        or returned_con_id <= 0
        for returned_con_id in returned_con_ids
    ):
        raise LookupError(
            f"option chain parameters for {symbol!r} returned an invalid underlyingConId"
        )
    if any(returned_con_id != con_id for returned_con_id in returned_con_ids):
        raise LookupError(
            f"option chain parameters for {symbol!r} returned conflicting "
            f"underlyingConId values {sorted(set(returned_con_ids))}; expected {con_id}"
        )
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
    return OptionChainParams(
        underlying_symbol=symbol,
        underlying_con_id=con_id,
        trading_class=chain.tradingClass,
        exchange=chain.exchange,
        multiplier=chain.multiplier,
        expirations=tuple(sorted(chain.expirations)),
        strikes=tuple(sorted(chain.strikes)),
        currency=(str(chain.currency) if getattr(chain, "currency", None) else None),
    )


# --- I/O: paced snapshot ---

_MISSING_SENTINEL = -1.0  # IBKR reports -1 (or non-finite) for "no quote yet"


def _finite_or_none(x) -> float | None:
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf) or xf <= _MISSING_SENTINEL:
        return None
    return xf


def _utc_timestamp(value) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _ticker_market_evidence(ticker) -> tuple[str, int, str] | None:
    market_data_type = getattr(ticker, "marketDataType", None)
    if (
        not isinstance(market_data_type, int)
        or isinstance(market_data_type, bool)
        or market_data_type not in {1, 2, 3, 4}
    ):
        return None
    received_at = _utc_timestamp(getattr(ticker, "time", None))
    if received_at is None:
        return None
    if market_data_type == 1:
        observed_at = received_at
    elif market_data_type == 2:
        observed_at = _utc_timestamp(getattr(ticker, "lastTimestamp", None))
    else:
        observed_at = _utc_timestamp(
            getattr(ticker, "delayedLastTimestamp", None)
        )
    if observed_at is None or observed_at > received_at:
        return None
    timestamp = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    received_timestamp = received_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return timestamp, market_data_type, received_timestamp


def _ticker_to_quote(underlier: str, ticker) -> OptionQuote | None:
    evidence = _ticker_market_evidence(ticker)
    if evidence is None:
        return None
    observed_at, market_data_type, received_at = evidence
    c = ticker.contract
    greeks = ticker.modelGreeks or ticker.lastGreeks or ticker.bidGreeks or ticker.askGreeks
    iv = delta = None
    if greeks is not None:
        iv = _finite_or_none(greeks.impliedVol)
        delta = _finite_or_none(greeks.delta)
    if iv is None:
        iv = _finite_or_none(getattr(ticker, "impliedVolatility", None))
    multiplier = float(c.multiplier) if c.multiplier else 100.0
    return OptionQuote(
        underlier=underlier,
        expiry=c.lastTradeDateOrContractMonth,
        strike=float(c.strike),
        right=c.right,
        con_id=getattr(c, "conId", None) or None,
        bid=_finite_or_none(ticker.bid),
        ask=_finite_or_none(ticker.ask),
        iv=iv,
        delta=delta,
        multiplier=multiplier,
        observed_at=observed_at,
        market_data_type=market_data_type,
        received_at=received_at,
    )


def _contract_terms(contract) -> tuple[str, str, float, str] | None:
    symbol = getattr(contract, "symbol", None)
    expiry = getattr(contract, "lastTradeDateOrContractMonth", None)
    right = getattr(contract, "right", None)
    if not isinstance(symbol, str) or not symbol:
        return None
    if not isinstance(expiry, str):
        return None
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError:
        return None
    try:
        raw_strike = getattr(contract, "strike", None)
        if isinstance(raw_strike, bool):
            return None
        strike = float(raw_strike)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(strike) or strike <= 0:
        return None
    if right not in {"C", "P"}:
        return None
    return symbol, expiry, strike, right


def _contract_identity(contract) -> tuple[int, str, str, float, str] | None:
    con_id = getattr(contract, "conId", None)
    if not isinstance(con_id, int) or isinstance(con_id, bool) or con_id <= 0:
        return None
    terms = _contract_terms(contract)
    return (con_id, *terms) if terms is not None else None


def _chain_contract_matches(
    chain: OptionChainParams,
    requested_terms: set[tuple[str, str, float, str]],
    contract,
) -> bool:
    identity = _contract_identity(contract)
    if identity is None or identity[1:] not in requested_terms:
        return False
    sec_type = getattr(contract, "secType", "")
    if sec_type != "OPT":
        return False
    trading_class = getattr(contract, "tradingClass", "")
    if trading_class != chain.trading_class:
        return False
    multiplier = getattr(contract, "multiplier", "")
    try:
        if float(multiplier) != float(chain.multiplier):
            return False
    except (TypeError, ValueError):
        return False
    currency = getattr(contract, "currency", "")
    if chain.currency and currency != chain.currency:
        return False
    return True


def _qualified_contract_identity(contract) -> tuple | None:
    identity = _contract_identity(contract)
    if identity is None:
        return None
    return (
        *identity,
        str(getattr(contract, "tradingClass", "")),
        str(getattr(contract, "multiplier", "")),
        str(getattr(contract, "currency", "")),
    )


async def snapshot_option_quotes(
    ib,
    chain: OptionChainParams,
    expiries: Sequence[str],
    strikes: Sequence[float],
    sleep=asyncio.sleep,
    pace_seconds: float = 1.0,
    batch_size: int = 50,
    market_data_type: int = 4,
) -> list[OptionQuote]:
    """Builds Option contracts for every (expiry, strike, right) in
    `expiries` x `strikes` x {C, P}, then qualifies + snapshots them in paced
    batches (Engineering Constraint 6's pacing discipline extended to OPRA
    market data): each batch is `qualifyContractsAsync` then `reqTickersAsync`,
    followed by a `sleep(pace_seconds)` before the next batch — one full pause
    per batch, not per contract, keeps a 90-day/±15% chain to a small number of
    paced round trips. Contracts IB can't resolve (delisted strike, bad
    combination) are dropped, never raised — one bad strike must not abort the
    whole chain sync."""
    from ib_async import Option

    # Market-data type 4 = delayed-frozen: last available delayed quote, served
    # even off-hours and WITHOUT live OPRA sharing on the session (field report
    # 2026-07-26: Error 354 on every live option snapshot from the paper
    # session, with "Delayed market data is available"). The chain cache feeds
    # daily risk math, so delayed is honest; sessions with live sharing enabled
    # can pass market_data_type=1.
    if hasattr(ib, "reqMarketDataType"):
        ib.reqMarketDataType(market_data_type)

    contracts = [
        Option(
            chain.underlying_symbol,
            expiry,
            strike,
            right,
            chain.exchange,
            chain.multiplier,
            chain.currency or "",
            tradingClass=chain.trading_class,
        )
        for expiry in expiries
        for strike in strikes
        for right in ("C", "P")
    ]

    quotes: list[OptionQuote] = []
    for i in range(0, len(contracts), batch_size):
        batch = contracts[i : i + batch_size]
        requested_terms = {
            terms
            for contract in batch
            if (terms := _contract_terms(contract)) is not None
        }
        qualified_raw = await ib.qualifyContractsAsync(*batch)
        qualified = [
            contract
            for contract in qualified_raw
            if contract is not None
            and _chain_contract_matches(chain, requested_terms, contract)
        ]
        if qualified:
            qualified_identities = {
                identity
                for contract in qualified
                if (identity := _qualified_contract_identity(contract)) is not None
            }
            tickers = await ib.reqTickersAsync(*qualified)
            for ticker in tickers:
                if (
                    _qualified_contract_identity(ticker.contract)
                    not in qualified_identities
                ):
                    continue
                quote = _ticker_to_quote(ticker.contract.symbol, ticker)
                if quote is not None:
                    quotes.append(quote)
        await sleep(pace_seconds)
    return quotes


async def snapshot_held_option_quotes(
    ib,
    positions: Sequence,
    sleep=asyncio.sleep,
    pace_seconds: float = 1.0,
    batch_size: int = 50,
    market_data_type: int = 4,
) -> list[OptionQuote]:
    """Snapshot exact held contracts, including weeklies, LEAPS and far strikes.

    The authoritative IBKR conId is carried onto each request. Surface sampling
    remains bounded, while the book itself is never excluded by that sample.
    """
    from ib_async import Option

    if hasattr(ib, "reqMarketDataType"):
        ib.reqMarketDataType(market_data_type)

    contracts = []
    for position in positions:
        if (
            getattr(position, "sec_type", None) != "OPT"
            or getattr(position, "strike", None) is None
            or getattr(position, "expiry", None) is None
            or getattr(position, "right", None) not in {"C", "P"}
        ):
            continue
        contract = Option(
            position.symbol,
            position.expiry,
            position.strike,
            position.right,
            getattr(position, "exchange", None) or "SMART",
            str(position.multiplier),
            getattr(position, "currency", None) or "USD",
        )
        contract.conId = position.con_id
        contracts.append(contract)

    quotes: list[OptionQuote] = []
    for i in range(0, len(contracts), batch_size):
        batch = contracts[i : i + batch_size]
        requested_identities = {
            identity
            for contract in batch
            if (identity := _contract_identity(contract)) is not None
        }
        qualified_raw = await ib.qualifyContractsAsync(*batch)
        qualified = [
            contract
            for contract in qualified_raw
            if contract is not None
            and _contract_identity(contract) in requested_identities
        ]
        if qualified:
            tickers = await ib.reqTickersAsync(*qualified)
            for ticker in tickers:
                if _contract_identity(ticker.contract) not in requested_identities:
                    continue
                quote = _ticker_to_quote(ticker.contract.symbol, ticker)
                if quote is not None:
                    quotes.append(quote)
        await sleep(pace_seconds)
    return quotes
