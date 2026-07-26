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
from datetime import date, datetime
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
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
    return OptionChainParams(
        underlying_symbol=symbol,
        underlying_con_id=con_id,
        trading_class=chain.tradingClass,
        exchange=chain.exchange,
        multiplier=chain.multiplier,
        expirations=tuple(sorted(chain.expirations)),
        strikes=tuple(sorted(chain.strikes)),
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


def _ticker_to_quote(underlier: str, ticker) -> OptionQuote:
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
        Option(chain.underlying_symbol, expiry, strike, right, chain.exchange, chain.multiplier)
        for expiry in expiries
        for strike in strikes
        for right in ("C", "P")
    ]

    quotes: list[OptionQuote] = []
    for i in range(0, len(contracts), batch_size):
        batch = contracts[i : i + batch_size]
        qualified_raw = await ib.qualifyContractsAsync(*batch)
        qualified = [c for c in qualified_raw if c is not None and getattr(c, "conId", None)]
        if qualified:
            tickers = await ib.reqTickersAsync(*qualified)
            quotes.extend(_ticker_to_quote(chain.underlying_symbol, t) for t in tickers)
        await sleep(pace_seconds)
    return quotes
