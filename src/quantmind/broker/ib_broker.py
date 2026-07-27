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


def positions_to_cost_basis(ib_positions) -> dict[int, float]:
    """Map ib_async position objects to {con_id: avgCost} (Task B1 — ledger
    essentials: cost basis for unrealized P&L). Mirrors `positions_to_portfolio`'s
    zero-qty drop exactly (a closed-during-session row is not a holding, so it
    has no cost basis to report either) — the two functions are always called
    together over the SAME `reqPositionsAsync()` result and must agree on
    which rows count as positions."""
    return {p.contract.conId: float(p.avgCost) for p in ib_positions if p.position != 0}


def positions_to_portfolio(ib_positions, as_of: str) -> Portfolio:
    """Map ib_async position objects to our Portfolio. Zero-qty entries are dropped
    (IBKR reports positions closed during the session as qty 0)."""
    positions = []
    for p in ib_positions:
        if p.position == 0:
            continue
        raw_mult = getattr(p.contract, "multiplier", "") or ""
        multiplier = float(raw_mult) if str(raw_mult).strip() else 1.0
        # Option contract terms (2026-07-27 live-account incident: real legs
        # pinned with null strike/expiry/right because these were never read
        # off the contract). ib_async's non-option sentinels — strike 0.0,
        # empty strings — map to honest Nones, never a phantom 0.0-strike leg.
        raw_strike = getattr(p.contract, "strike", 0.0) or 0.0
        raw_expiry = str(getattr(p.contract, "lastTradeDateOrContractMonth", "") or "").strip()
        raw_right = str(getattr(p.contract, "right", "") or "").strip()
        positions.append(
            Position(
                con_id=p.contract.conId,
                symbol=p.contract.symbol,
                qty=float(p.position),
                sec_type=p.contract.secType,
                multiplier=multiplier,
                strike=float(raw_strike) if raw_strike else None,
                expiry=raw_expiry or None,
                right=raw_right or None,
            )
        )
    return Portfolio(positions=tuple(positions), as_of=as_of)


class IbBroker(ReadOnlyBroker):
    def __init__(self, ib):
        self._ib = ib

    async def get_portfolio(self) -> Portfolio:
        ib_positions = await self._ib.reqPositionsAsync()
        return positions_to_portfolio(ib_positions, as_of=str(date.today()))

    async def get_avg_costs(self) -> dict[int, float]:
        """Cost basis per con_id (Task B1 — ledger essentials). A second
        `reqPositionsAsync()` call rather than folding into `get_portfolio`:
        `Portfolio`/`Position` (Engineering Constraint 9's one Portfolio type)
        has no room for avgCost, so this stays a sibling read rather than
        widening that shared type."""
        ib_positions = await self._ib.reqPositionsAsync()
        return positions_to_cost_basis(ib_positions)

    # Account-summary tags this dashboard surfaces today (Task B1's "ledger
    # essentials"); mapped to snake_case response keys. Extend this dict
    # (never widen the wire-format tag string above it) to add more later.
    _ACCOUNT_SUMMARY_TAGS = {
        "NetLiquidation": "net_liquidation",
        "TotalCashValue": "total_cash_value",
        "GrossPositionValue": "gross_position_value",
        "BuyingPower": "buying_power",
    }

    async def get_account_summary(self) -> dict[str, float | None]:
        """NetLiquidation/TotalCashValue/GrossPositionValue/BuyingPower via
        `reqAccountSummaryAsync` (Task B1). ib_async's `reqAccountSummaryAsync`
        requests the FULL fixed tag set and returns `None`, populating
        `ib.accountSummary()` as a side effect (its own documented shape) — a
        tag absent from the response, or present with an unparseable value,
        maps to an honest `None` rather than a fabricated 0.0.

        Subscribe-once (2026-07-27 live-account incident, Error 322): IBKR
        caps concurrent account-summary subscriptions per session and the
        subscription live-updates on its own — so request only when no
        values exist yet (fresh session or post-reconnect), read thereafter."""
        if not self._ib.accountSummary():
            await self._ib.reqAccountSummaryAsync()
        values = self._ib.accountSummary()
        out: dict[str, float | None] = {key: None for key in self._ACCOUNT_SUMMARY_TAGS.values()}
        for v in values:
            key = self._ACCOUNT_SUMMARY_TAGS.get(v.tag)
            if key is None:
                continue
            try:
                out[key] = float(v.value)
            except (TypeError, ValueError):
                out[key] = None
        return out

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

    async def resolve_index_con_id(self, symbol: str, exchange: str = "CBOE") -> int:
        """Indices (VIX, SPX) aren't SMART-routable — resolve via an Index
        contract on the primary exchange. Empirically verified working:
        Index("VIX", "CBOE") (Task A2 design note)."""
        from ib_async import Index

        contract = Index(symbol, exchange)
        details = await self._ib.reqContractDetailsAsync(contract)
        if not details:
            raise LookupError(f"could not resolve index contract for symbol {symbol!r}")
        return details[0].contract.conId

    async def get_index_bars(self, con_id: int, exchange: str = "CBOE", years: int = 5) -> pd.DataFrame:
        """Indices have no ADJUSTED_LAST feed (no splits/dividends to adjust
        for), so this fetches TRADES bars — the whatToShow that was
        empirically verified to work for VIX/SPX Index contracts."""
        from ib_async import Contract, util

        contract = Contract(conId=con_id, exchange=exchange, secType="IND")
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{years} Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        df = util.df(bars)
        if df is None or df.empty:
            raise LookupError(f"no historical index bars returned for con_id {con_id}")
        df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["date"])))
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    async def fetch_contract_details(self, con_id: int) -> dict:
        """Contract-details metadata cache (Task A2): longName/exchange/
        currency/secType/industry, keyed by conId (resolves by conId alone —
        works uniformly for stocks, ETFs, and indices)."""
        from ib_async import Contract

        contract = Contract(conId=con_id)
        details = await self._ib.reqContractDetailsAsync(contract)
        if not details:
            raise LookupError(f"could not fetch contract details for con_id {con_id}")
        d = details[0]
        return {
            "long_name": d.longName or None,
            "exchange": d.contract.exchange or None,
            "currency": d.contract.currency or None,
            "sec_type": d.contract.secType or None,
            "industry": (d.industry or None) if hasattr(d, "industry") else None,
        }
