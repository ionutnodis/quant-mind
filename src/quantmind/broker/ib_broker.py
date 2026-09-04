"""Thin ib_async implementation of the read-only broker surface.

Mapping logic is pure and unit-tested; the network methods are covered by the
opt-in E2E smoke test. Bars are requested as ADJUSTED_LAST daily bars
(Engineering Constraint 3) — split/dividend-adjusted, RTH only.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd

from quantmind.broker.base import ReadOnlyBroker
from quantmind.portfolio import Portfolio, Position


class AccountSelectionError(ValueError):
    """The IBKR session exposes no unambiguous single account."""


def _contract_multiplier(contract) -> float:
    raw_multiplier = getattr(contract, "multiplier", "") or ""
    if not str(raw_multiplier).strip():
        if contract.secType == "OPT":
            raise ValueError(
                f"option contract {contract.conId} has no valid multiplier"
            )
        return 1.0
    multiplier = float(raw_multiplier)
    if contract.secType == "OPT" and multiplier <= 0:
        raise ValueError(
            f"option contract {contract.conId} has no valid multiplier"
        )
    return multiplier


def positions_to_cost_basis(ib_positions) -> dict[int, float]:
    """Map ib_async position objects to {con_id: avgCost} (Task B1 — ledger
    essentials: per-unit cost basis for unrealized P&L). IBKR reports option
    avgCost per contract, including its multiplier, while option quotes are
    per underlying unit. Normalize options here so every consumer receives
    cost and mark in matching units."""
    costs: dict[int, float] = {}
    for position in ib_positions:
        if position.position == 0:
            continue
        avg_cost = float(
            getattr(position, "avgCost", getattr(position, "averageCost", 0.0))
        )
        if position.contract.secType == "OPT":
            avg_cost /= _contract_multiplier(position.contract)
        costs[position.contract.conId] = avg_cost
    return costs


def positions_to_portfolio(ib_positions, as_of: str) -> Portfolio:
    """Map ib_async position objects to our Portfolio. Zero-qty entries are dropped
    (IBKR reports positions closed during the session as qty 0)."""
    positions = []
    for p in ib_positions:
        if p.position == 0:
            continue
        multiplier = _contract_multiplier(p.contract)
        is_option = p.contract.secType == "OPT"
        raw_strike = getattr(p.contract, "strike", None) if is_option else None
        strike = float(raw_strike) if raw_strike not in (None, "", 0, 0.0) else None
        raw_expiry = str(
            getattr(p.contract, "lastTradeDateOrContractMonth", "") or ""
        ).strip()
        expiry = raw_expiry[:8] if is_option and raw_expiry[:8].isdigit() else None
        raw_right = getattr(p.contract, "right", None) if is_option else None
        right = raw_right if raw_right in {"C", "P"} else None
        # IBKR contract identity should always carry a currency. Preserve an
        # explicit sentinel when it does not so downstream USD-only guards
        # fail closed instead of silently treating an unknown mark as USD.
        currency = str(getattr(p.contract, "currency", "") or "").strip() or "UNKNOWN"
        exchange = str(
            getattr(p.contract, "primaryExchange", "")
            or getattr(p.contract, "exchange", "")
            or ""
        ).strip() or None
        positions.append(
            Position(
                con_id=p.contract.conId,
                symbol=p.contract.symbol,
                qty=float(p.position),
                sec_type=p.contract.secType,
                multiplier=multiplier,
                strike=strike,
                expiry=expiry,
                right=right,
                currency=currency,
                exchange=exchange,
            )
        )
    return Portfolio(positions=tuple(positions), as_of=as_of)


class IbBroker(ReadOnlyBroker):
    def __init__(self, ib, account_id: str = ""):
        self._ib = ib
        self._account_id = account_id
        self._account_updates_started: str | None = None
        self._account_updates_lock = asyncio.Lock()

    def _selected_account(self) -> str:
        managed_accounts = (
            list(self._ib.managedAccounts())
            if hasattr(self._ib, "managedAccounts")
            else []
        )
        if self._account_id:
            if managed_accounts and self._account_id not in managed_accounts:
                raise AccountSelectionError(
                    f"QM_ACCOUNT_ID does not match an account visible to this IBKR session"
                )
            return self._account_id
        if len(managed_accounts) > 1:
            raise AccountSelectionError(
                "multiple IBKR accounts are visible; set QM_ACCOUNT_ID to select exactly one"
            )
        return managed_accounts[0] if managed_accounts else ""

    @property
    def selected_account_id(self) -> str:
        """The validated account scope; reads only IB's local session state."""
        return self._selected_account()

    def _for_selected_account(self, values):
        account_id = self._selected_account()
        if not account_id:
            return list(values)
        return [value for value in values if getattr(value, "account", "") == account_id]

    async def _selected_positions(self):
        """Return one account's streaming portfolio without global FA reads.

        `reqPositions` is unavailable to advisor/introducing-broker masters
        with more than 50 subaccounts. IBKR's single-account update stream is
        supported for that shape and populates `IB.portfolio(account)`. Keep
        the legacy request only as a compatibility fallback for small/test
        clients that do not expose the account-scoped API.
        """
        account_id = self._selected_account()
        scoped_supported = (
            bool(account_id)
            and hasattr(self._ib, "reqAccountUpdatesAsync")
            and hasattr(self._ib, "portfolio")
        )
        if not scoped_supported:
            values = await self._ib.reqPositionsAsync()
            return self._for_selected_account(values)

        async with self._account_updates_lock:
            if self._account_updates_started != account_id:
                await self._ib.reqAccountUpdatesAsync(account_id)
                self._account_updates_started = account_id
        return list(self._ib.portfolio(account_id))

    async def get_portfolio(self) -> Portfolio:
        ib_positions = await self._selected_positions()
        return positions_to_portfolio(
            ib_positions, as_of=str(date.today())
        )

    async def get_avg_costs(self) -> dict[int, float]:
        """Cost basis per con_id (Task B1 — ledger essentials). A second
        `reqPositionsAsync()` call rather than folding into `get_portfolio`:
        `Portfolio`/`Position` (Engineering Constraint 9's one Portfolio type)
        has no room for avgCost, so this stays a sibling read rather than
        widening that shared type."""
        return positions_to_cost_basis(await self._selected_positions())

    # Account-summary tags this dashboard surfaces today (Task B1's "ledger
    # essentials"); mapped to snake_case response keys. Extend this dict
    # (never widen the wire-format tag string above it) to add more later.
    _ACCOUNT_SUMMARY_TAGS = {
        "NetLiquidation": "net_liquidation",
        "TotalCashValue": "total_cash_value",
        "GrossPositionValue": "gross_position_value",
        "BuyingPower": "buying_power",
    }

    async def get_account_summary(self) -> dict[str, float | str | None]:
        """NetLiquidation/TotalCashValue/GrossPositionValue/BuyingPower via
        `reqAccountSummaryAsync` (Task B1). ib_async's `reqAccountSummaryAsync`
        requests the FULL fixed tag set and returns `None`, populating
        `ib.accountSummary()` as a side effect (its own documented shape) — a
        tag absent from the response, or present with an unparseable value,
        maps to an honest `None` rather than a fabricated 0.0."""
        account_id = self._selected_account()
        if (
            account_id
            and hasattr(self._ib, "reqAccountUpdatesAsync")
            and hasattr(self._ib, "accountValues")
        ):
            await self._selected_positions()
            values = list(self._ib.accountValues(account_id))
        else:
            await self._ib.reqAccountSummaryAsync()
            values = self._for_selected_account(self._ib.accountSummary())
        out: dict[str, float | str | None] = {
            key: None for key in self._ACCOUNT_SUMMARY_TAGS.values()
        }
        candidates: dict[str, list[object]] = {
            key: [] for key in self._ACCOUNT_SUMMARY_TAGS.values()
        }
        for v in values:
            key = self._ACCOUNT_SUMMARY_TAGS.get(v.tag)
            if key is None:
                continue
            candidates[key].append(v)

        currencies: set[str] = set()
        for key, rows in candidates.items():
            if not rows:
                continue
            base_rows = [
                row
                for row in rows
                if str(getattr(row, "currency", "") or "").strip() == "BASE"
            ]
            if base_rows:
                selected = base_rows[-1]
            else:
                row_currencies = {
                    str(getattr(row, "currency", "") or "").strip()
                    for row in rows
                    if str(getattr(row, "currency", "") or "").strip()
                }
                if len(row_currencies) > 1:
                    continue
                selected = rows[-1]
                currencies.update(row_currencies)
            try:
                out[key] = float(selected.value)
            except (TypeError, ValueError):
                out[key] = None
        out["currency"] = next(iter(currencies)) if len(currencies) == 1 else None
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
