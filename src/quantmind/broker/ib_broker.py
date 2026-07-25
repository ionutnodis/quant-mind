"""Thin ib_async implementation of the read-only broker surface.

Mapping logic is pure and unit-tested; the network methods are covered by the
opt-in E2E smoke test. Bars are requested as ADJUSTED_LAST daily bars
(Engineering Constraint 3) — split/dividend-adjusted, RTH only.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from quantmind.broker.base import ReadOnlyBroker
from quantmind.portfolio import Portfolio, Position


def positions_to_portfolio(ib_positions, as_of: str) -> Portfolio:
    """Map ib_async position objects to our Portfolio. Zero-qty entries are dropped
    (IBKR reports positions closed during the session as qty 0)."""
    positions = []
    for p in ib_positions:
        if p.position == 0:
            continue
        raw_mult = getattr(p.contract, "multiplier", "") or ""
        multiplier = float(raw_mult) if str(raw_mult).strip() else 1.0
        positions.append(
            Position(
                con_id=p.contract.conId,
                symbol=p.contract.symbol,
                qty=float(p.position),
                sec_type=p.contract.secType,
                multiplier=multiplier,
            )
        )
    return Portfolio(positions=tuple(positions), as_of=as_of)


class IbBroker(ReadOnlyBroker):
    def __init__(self, ib):
        self._ib = ib

    async def get_portfolio(self) -> Portfolio:
        ib_positions = await self._ib.reqPositionsAsync()
        return positions_to_portfolio(ib_positions, as_of=str(date.today()))

    async def resolve_stock_con_id(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> int:
        from ib_async import Stock

        contract = Stock(symbol, exchange, currency)
        details = await self._ib.reqContractDetailsAsync(contract)
        if not details:
            raise LookupError(f"could not resolve contract for symbol {symbol!r}")
        return details[0].contract.conId

    async def get_daily_bars(self, con_id: int, years: int = 5) -> pd.DataFrame:
        from ib_async import Contract, util

        contract = Contract(conId=con_id, exchange="SMART")
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{years} Y",
            barSizeSetting="1 day",
            whatToShow="ADJUSTED_LAST",
            useRTH=True,
            formatDate=1,
        )
        df = util.df(bars)
        if df is None or df.empty:
            raise LookupError(f"no historical bars returned for con_id {con_id}")
        df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["date"])))
        return df[["open", "high", "low", "close", "volume"]].astype(float)
