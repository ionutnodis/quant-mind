"""Mapping logic from ib_async objects to our Portfolio — tested with fakes.

The network-touching paths are covered by the opt-in E2E smoke test, except
for the Task A2 additions below (resolve_index_con_id / get_index_bars /
fetch_contract_details), whose ib_async-object mapping is exercised here
against a FakeIB stub (pattern: tests/test_broker_connection.py) — no network,
real ib_async dataclasses so `util.df` behaves exactly as in production.
"""

from types import SimpleNamespace

import pandas as pd
import pytest
from ib_async import BarData, Contract, ContractDetails

from quantmind.broker.ib_broker import IbBroker, positions_to_portfolio


def _ib_pos(con_id, symbol, qty, sec_type="STK", multiplier=""):
    contract = SimpleNamespace(conId=con_id, symbol=symbol, secType=sec_type, multiplier=multiplier)
    return SimpleNamespace(contract=contract, position=qty, avgCost=0.0)


def test_stock_position_maps_with_unit_multiplier():
    p = positions_to_portfolio([_ib_pos(265598, "AAPL", 100.0)], as_of="2026-07-25")
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos.con_id == 265598
    assert pos.symbol == "AAPL"
    assert pos.qty == 100.0
    assert pos.sec_type == "STK"
    assert pos.multiplier == 1.0


def test_option_position_maps_string_multiplier_to_float():
    p = positions_to_portfolio(
        [_ib_pos(777, "AAPL", -2.0, sec_type="OPT", multiplier="100")], as_of="2026-07-25"
    )
    pos = p.positions[0]
    assert pos.sec_type == "OPT"
    assert pos.multiplier == 100.0
    assert pos.qty == -2.0


def test_empty_or_blank_multiplier_defaults_to_one():
    p = positions_to_portfolio([_ib_pos(1, "X", 1.0, multiplier="")], as_of="2026-07-25")
    assert p.positions[0].multiplier == 1.0


def test_zero_quantity_positions_are_dropped():
    # IBKR reports closed positions as qty 0 for the session — they are not holdings
    p = positions_to_portfolio(
        [_ib_pos(1, "X", 0.0), _ib_pos(2, "Y", 5.0)], as_of="2026-07-25"
    )
    assert [pos.con_id for pos in p.positions] == [2]


# --- Task A2: index resolution, index bars, contract-details metadata ---


class FakeIB:
    """Async ib_async.IB stub returning canned ContractDetails/BarData."""

    def __init__(self, contract_details=None, bars=None):
        self._contract_details = contract_details or []
        self._bars = bars if bars is not None else []
        self.reqContractDetails_calls = []
        self.reqHistoricalData_calls = []

    async def reqContractDetailsAsync(self, contract):
        self.reqContractDetails_calls.append(contract)
        return self._contract_details

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.reqHistoricalData_calls.append((contract, kwargs))
        return self._bars


def _vix_contract_details(con_id=13455763):
    return ContractDetails(
        contract=Contract(conId=con_id, symbol="VIX", secType="IND", exchange="CBOE", currency="USD"),
        longName="CBOE Volatility Index",
        industry="",
    )


def _index_bars(n=5):
    idx = pd.bdate_range("2026-01-05", periods=n)
    return [
        BarData(date=d.strftime("%Y%m%d"), open=15.0 + i, high=15.5 + i, low=14.5 + i, close=15.2 + i, volume=0)
        for i, d in enumerate(idx)
    ]


async def test_resolve_index_con_id_returns_conid_from_contract_details():
    ib = FakeIB(contract_details=[_vix_contract_details()])
    broker = IbBroker(ib)
    con_id = await broker.resolve_index_con_id("VIX", "CBOE")
    assert con_id == 13455763
    (contract,) = [c for c in [ib.reqContractDetails_calls[0]]]
    assert contract.symbol == "VIX"
    assert contract.exchange == "CBOE"
    assert contract.secType == "IND"


async def test_resolve_index_con_id_raises_lookup_error_when_unresolvable():
    ib = FakeIB(contract_details=[])
    broker = IbBroker(ib)
    with pytest.raises(LookupError, match="VIX"):
        await broker.resolve_index_con_id("VIX", "CBOE")


async def test_get_index_bars_uses_trades_not_adjusted_last():
    ib = FakeIB(bars=_index_bars())
    broker = IbBroker(ib)
    df = await broker.get_index_bars(13455763, "CBOE", years=1)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5
    _, kwargs = ib.reqHistoricalData_calls[0]
    assert kwargs["whatToShow"] == "TRADES"


async def test_get_index_bars_raises_lookup_error_on_empty_result():
    ib = FakeIB(bars=[])
    broker = IbBroker(ib)
    with pytest.raises(LookupError, match="13455763"):
        await broker.get_index_bars(13455763, "CBOE")


async def test_fetch_contract_details_maps_expected_fields():
    ib = FakeIB(contract_details=[_vix_contract_details()])
    broker = IbBroker(ib)
    meta = await broker.fetch_contract_details(13455763)
    assert meta == {
        "long_name": "CBOE Volatility Index",
        "exchange": "CBOE",
        "currency": "USD",
        "sec_type": "IND",
        "industry": None,  # blank string normalizes to None
    }


async def test_fetch_contract_details_raises_lookup_error_when_unresolvable():
    ib = FakeIB(contract_details=[])
    broker = IbBroker(ib)
    with pytest.raises(LookupError, match="42"):
        await broker.fetch_contract_details(42)
