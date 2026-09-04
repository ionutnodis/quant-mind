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
from ib_async import AccountValue, BarData, Contract, ContractDetails

from quantmind.broker.ib_broker import IbBroker, positions_to_cost_basis, positions_to_portfolio


def _ib_pos(
    con_id,
    symbol,
    qty,
    sec_type="STK",
    multiplier="",
    avg_cost=0.0,
    strike=0.0,
    expiry="",
    right="",
    account="",
    currency="",
    exchange="",
):
    contract = SimpleNamespace(
        conId=con_id,
        symbol=symbol,
        secType=sec_type,
        multiplier=multiplier,
        strike=strike,
        lastTradeDateOrContractMonth=expiry,
        right=right,
        currency=currency,
        exchange=exchange,
    )
    return SimpleNamespace(contract=contract, position=qty, avgCost=avg_cost, account=account)


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


def test_option_position_without_multiplier_fails_closed():
    with pytest.raises(ValueError, match="option contract 777.*multiplier"):
        positions_to_portfolio(
            [_ib_pos(777, "AAPL", -2.0, sec_type="OPT", multiplier="")],
            as_of="2026-07-25",
        )


def test_option_position_preserves_contract_terms_needed_to_reprice_the_live_book():
    portfolio = positions_to_portfolio(
        [
            _ib_pos(
                777,
                "AAPL",
                -2.0,
                sec_type="OPT",
                multiplier="100",
                strike=250.0,
                expiry="20261218",
                right="C",
            )
        ],
        as_of="2026-09-04",
    )

    position = portfolio.positions[0]
    assert position.strike == 250.0
    assert position.expiry == "20261218"
    assert position.right == "C"


def test_position_preserves_ibkr_currency_and_exchange_identity():
    portfolio = positions_to_portfolio(
        [
            _ib_pos(
                12345,
                "ASML",
                12,
                currency="EUR",
                exchange="AEB",
            )
        ],
        as_of="2026-09-04",
    )

    position = portfolio.positions[0]
    assert position.currency == "EUR"
    assert position.exchange == "AEB"


def test_position_leaves_missing_ibkr_identity_unset_for_legacy_fakes():
    portfolio = positions_to_portfolio(
        [_ib_pos(265598, "AAPL", 100)], as_of="2026-09-04"
    )

    position = portfolio.positions[0]
    assert position.currency == "UNKNOWN"
    assert position.exchange is None


def test_empty_or_blank_multiplier_defaults_to_one():
    p = positions_to_portfolio([_ib_pos(1, "X", 1.0, multiplier="")], as_of="2026-07-25")
    assert p.positions[0].multiplier == 1.0


def test_zero_quantity_positions_are_dropped():
    # IBKR reports closed positions as qty 0 for the session — they are not holdings
    p = positions_to_portfolio(
        [_ib_pos(1, "X", 0.0), _ib_pos(2, "Y", 5.0)], as_of="2026-07-25"
    )
    assert [pos.con_id for pos in p.positions] == [2]


# --- Task B1: cost basis (avgCost) mapping ---


def test_positions_to_cost_basis_maps_con_id_to_avg_cost():
    costs = positions_to_cost_basis(
        [_ib_pos(1, "SPY", 10.0, avg_cost=450.25), _ib_pos(2, "AAPL", 5.0, avg_cost=190.0)]
    )
    assert costs == {1: 450.25, 2: 190.0}


def test_positions_to_cost_basis_normalizes_option_contract_cost_to_unit_premium():
    costs = positions_to_cost_basis(
        [
            _ib_pos(
                777,
                "AAPL",
                2.0,
                sec_type="OPT",
                multiplier="100",
                avg_cost=4229.1637,
            )
        ]
    )

    assert costs == {777: pytest.approx(42.291637)}


def test_option_cost_basis_without_multiplier_fails_closed():
    with pytest.raises(ValueError, match="option contract 777.*multiplier"):
        positions_to_cost_basis(
            [
                _ib_pos(
                    777,
                    "AAPL",
                    2.0,
                    sec_type="OPT",
                    multiplier="",
                    avg_cost=4229.1637,
                )
            ]
        )


def test_positions_to_cost_basis_drops_zero_quantity_positions():
    # Matches positions_to_portfolio's own zero-qty drop (closed-during-session rows).
    costs = positions_to_cost_basis([_ib_pos(1, "X", 0.0, avg_cost=100.0)])
    assert costs == {}


# --- Task A2: index resolution, index bars, contract-details metadata ---


class FakeIB:
    """Async ib_async.IB stub returning canned ContractDetails/BarData."""

    def __init__(
        self,
        contract_details=None,
        bars=None,
        positions=None,
        account_values=None,
        managed_accounts=None,
    ):
        self._contract_details = contract_details or []
        self._bars = bars if bars is not None else []
        self._positions = positions if positions is not None else []
        self._account_values = account_values if account_values is not None else []
        self._managed_accounts = managed_accounts if managed_accounts is not None else []
        self.reqContractDetails_calls = []
        self.reqHistoricalData_calls = []
        self.reqAccountSummary_calls = 0

    async def reqContractDetailsAsync(self, contract):
        self.reqContractDetails_calls.append(contract)
        return self._contract_details

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.reqHistoricalData_calls.append((contract, kwargs))
        return self._bars

    async def reqPositionsAsync(self):
        return self._positions

    async def reqAccountSummaryAsync(self):
        self.reqAccountSummary_calls += 1
        return None

    def accountSummary(self, account=""):
        return self._account_values

    def managedAccounts(self):
        return self._managed_accounts


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


# --- Task B1: ledger essentials — account summary + avg-cost basis ---


async def test_get_avg_costs_maps_live_positions_via_reqPositionsAsync():
    ib = FakeIB(positions=[_ib_pos(1, "SPY", 10.0, avg_cost=450.25), _ib_pos(2, "AAPL", 5.0, avg_cost=190.0)])
    broker = IbBroker(ib)
    costs = await broker.get_avg_costs()
    assert costs == {1: 450.25, 2: 190.0}


async def test_get_avg_costs_empty_book_returns_empty_dict():
    ib = FakeIB(positions=[])
    broker = IbBroker(ib)
    assert await broker.get_avg_costs() == {}


async def test_configured_account_filters_positions_and_cost_basis():
    positions = [
        _ib_pos(1, "NVDA", 100, avg_cost=120.0, account="U111"),
        _ib_pos(2, "MU", 50, avg_cost=90.0, account="U222"),
    ]
    broker = IbBroker(
        FakeIB(positions=positions, managed_accounts=["U111", "U222"]),
        account_id="U222",
    )

    portfolio = await broker.get_portfolio()

    assert [position.symbol for position in portfolio.positions] == ["MU"]
    assert await broker.get_avg_costs() == {2: 90.0}


async def test_multiple_managed_accounts_without_selection_fail_closed():
    broker = IbBroker(
        FakeIB(
            positions=[
                _ib_pos(1, "NVDA", 100, account="U111"),
                _ib_pos(2, "MU", 50, account="U222"),
            ],
            managed_accounts=["U111", "U222"],
        )
    )

    with pytest.raises(ValueError, match="QM_ACCOUNT_ID"):
        await broker.get_portfolio()


async def test_configured_account_must_be_visible_to_the_ibkr_session():
    broker = IbBroker(
        FakeIB(
            positions=[_ib_pos(1, "NVDA", 100, account="U111")],
            managed_accounts=["U111"],
        ),
        account_id="U999",
    )

    with pytest.raises(ValueError, match="does not match"):
        await broker.get_portfolio()


async def test_selected_advisor_account_uses_account_scoped_portfolio_updates():
    class AdvisorIB(FakeIB):
        def __init__(self):
            super().__init__(managed_accounts=[f"U{i:03d}" for i in range(60)])
            self.requested_accounts = []

        async def reqPositionsAsync(self):
            raise AssertionError("global reqPositions is unsupported for 50+ accounts")

        async def reqAccountUpdatesAsync(self, account):
            self.requested_accounts.append(account)

        def portfolio(self, account=""):
            assert account == "U042"
            return [_ib_pos(42, "NVDA", 100, account=account, currency="USD")]

        def accountValues(self, account=""):
            return []

    ib = AdvisorIB()
    broker = IbBroker(ib, account_id="U042")

    portfolio = await broker.get_portfolio()
    costs = await broker.get_avg_costs()

    assert [position.symbol for position in portfolio.positions] == ["NVDA"]
    assert costs == {42: 0.0}
    assert ib.requested_accounts == ["U042"]


def _account_value(tag, value, currency="USD"):
    return AccountValue(account="DU1234567", tag=tag, value=value, currency=currency, modelCode="")


async def test_get_account_summary_maps_wanted_tags_to_floats():
    ib = FakeIB(
        account_values=[
            _account_value("NetLiquidation", "125000.50"),
            _account_value("TotalCashValue", "20000.00"),
            _account_value("GrossPositionValue", "105000.50"),
            _account_value("BuyingPower", "60000.00"),
            _account_value("SMA", "99999.99"),  # not in our wanted set — ignored
        ]
    )
    broker = IbBroker(ib)
    summary = await broker.get_account_summary()
    assert summary == {
        "net_liquidation": 125000.50,
        "total_cash_value": 20000.00,
        "gross_position_value": 105000.50,
        "buying_power": 60000.00,
        "currency": "USD",
    }
    assert ib.reqAccountSummary_calls == 1


async def test_get_account_summary_missing_tags_are_honest_none():
    ib = FakeIB(account_values=[_account_value("NetLiquidation", "125000.50")])
    broker = IbBroker(ib)
    summary = await broker.get_account_summary()
    assert summary["net_liquidation"] == 125000.50
    assert summary["total_cash_value"] is None
    assert summary["gross_position_value"] is None
    assert summary["buying_power"] is None


async def test_get_account_summary_unparseable_value_is_honest_none():
    ib = FakeIB(account_values=[_account_value("NetLiquidation", "not-a-number")])
    broker = IbBroker(ib)
    summary = await broker.get_account_summary()
    assert summary["net_liquidation"] is None


async def test_account_summary_uses_only_the_configured_account():
    ib = FakeIB(
        account_values=[
            AccountValue(account="U111", tag="NetLiquidation", value="100", currency="USD", modelCode=""),
            AccountValue(account="U222", tag="NetLiquidation", value="250", currency="USD", modelCode=""),
        ],
        managed_accounts=["U111", "U222"],
    )
    broker = IbBroker(ib, account_id="U222")

    summary = await broker.get_account_summary()

    assert summary["net_liquidation"] == 250.0


async def test_account_summary_prefers_base_totals_over_currency_ledgers():
    """Account updates may publish a base total plus per-currency ledgers.

    The dashboard must never replace the converted account total with
    whichever currency-specific row happened to arrive last.
    """
    ib = FakeIB(
        account_values=[
            _account_value("NetLiquidation", "125000.50", currency="BASE"),
            _account_value("TotalCashValue", "20000.00", currency="BASE"),
            _account_value("TotalCashValue", "18000.00", currency="USD"),
            _account_value("TotalCashValue", "15000.00", currency="HKD"),
        ]
    )

    summary = await IbBroker(ib).get_account_summary()

    assert summary["net_liquidation"] == 125000.50
    assert summary["total_cash_value"] == 20000.00
    assert summary["currency"] is None
