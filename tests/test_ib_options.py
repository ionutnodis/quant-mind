"""ib_options.py: chain-parameter fetch + paced snapshot, tested with fakes
(no real ib_async network calls — pattern: tests/test_sync.py's FakeBroker,
tests/test_ib_broker_mapping.py's SimpleNamespace contract fakes).

Pure selection helpers (monthly-expiry filter, ±15% strike band) are tested
without any IB object at all.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from quantmind.broker.ib_options import (
    OptionChainParams,
    fetch_chain_params,
    select_monthly_expiries,
    select_strikes_near_spot,
    snapshot_held_option_quotes,
    snapshot_option_quotes,
)

# --- pure selection helpers ---


def test_select_monthly_expiries_keeps_third_fridays_within_window():
    # 2026-08-21 is the third Friday of August 2026; 2026-08-07 is a weekly
    # (first Friday); 2026-11-20 is a third Friday but > 90d from 2026-07-25.
    expirations = ["20260807", "20260821", "20260918", "20261120"]
    out = select_monthly_expiries(expirations, as_of=date(2026, 7, 25), max_days=90)
    assert out == ["20260821", "20260918"]


def test_select_monthly_expiries_excludes_past_dates():
    out = select_monthly_expiries(["20260717"], as_of=date(2026, 7, 25), max_days=90)
    assert out == []


def test_select_strikes_near_spot_15_percent_band():
    strikes = [80.0, 86.0, 90.0, 100.0, 110.0, 114.0, 120.0]
    out = select_strikes_near_spot(strikes, spot=100.0, pct=0.15)
    # band is [85, 115]: 80 and 120 fall outside +/-15%
    assert out == [86.0, 90.0, 100.0, 110.0, 114.0]


def test_select_strikes_near_spot_empty_when_none_in_band():
    assert select_strikes_near_spot([50.0, 200.0], spot=100.0, pct=0.15) == []


# --- fetch_chain_params (fakes) ---


def _fake_option_chain(
    exchange="SMART",
    trading_class="SPY",
    multiplier="100",
    underlying_con_id=756733,
):
    return SimpleNamespace(
        exchange=exchange,
        underlyingConId=underlying_con_id,
        tradingClass=trading_class,
        multiplier=multiplier,
        expirations=["20260918", "20260821"],
        strikes=[440.0, 450.0, 460.0],
    )


class _FakeIbChainParams:
    def __init__(self, chains):
        self._chains = chains
        self.calls = []

    async def reqSecDefOptParamsAsync(self, symbol, exchange, sec_type, con_id):
        self.calls.append((symbol, exchange, sec_type, con_id))
        return self._chains


async def test_fetch_chain_params_prefers_smart_exchange():
    ib = _FakeIbChainParams([_fake_option_chain(exchange="CBOE"), _fake_option_chain(exchange="SMART")])
    params = await fetch_chain_params(ib, "SPY", 756733)
    assert isinstance(params, OptionChainParams)
    assert params.exchange == "SMART"
    assert params.expirations == ("20260821", "20260918")  # sorted
    assert params.strikes == (440.0, 450.0, 460.0)
    assert ib.calls == [("SPY", "", "STK", 756733)]


async def test_fetch_chain_params_falls_back_to_first_when_no_smart():
    ib = _FakeIbChainParams([_fake_option_chain(exchange="CBOE")])
    params = await fetch_chain_params(ib, "SPY", 756733)
    assert params.exchange == "CBOE"


async def test_fetch_chain_params_raises_when_no_chains_returned():
    ib = _FakeIbChainParams([])
    with pytest.raises(LookupError):
        await fetch_chain_params(ib, "SPY", 756733)


@pytest.mark.parametrize("returned_con_id", [None, 0, -1, 999999])
async def test_fetch_chain_params_rejects_missing_or_conflicting_underlier_identity(
    returned_con_id,
):
    ib = _FakeIbChainParams(
        [_fake_option_chain(underlying_con_id=returned_con_id)]
    )

    with pytest.raises(LookupError, match="underlyingConId"):
        await fetch_chain_params(ib, "SPY", 756733)


async def test_fetch_chain_params_rejects_conflicting_identity_across_venues():
    ib = _FakeIbChainParams(
        [
            _fake_option_chain(exchange="SMART", underlying_con_id=756733),
            _fake_option_chain(exchange="CBOE", underlying_con_id=999999),
        ]
    )

    with pytest.raises(LookupError, match="conflicting"):
        await fetch_chain_params(ib, "SPY", 756733)


# --- snapshot_option_quotes (fakes, pacing) ---


def _fake_contract(symbol, expiry, strike, right, con_id, multiplier="100"):
    return SimpleNamespace(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        conId=con_id,
        multiplier=multiplier,
    )


def _fake_greeks(iv, delta):
    return SimpleNamespace(impliedVol=iv, delta=delta, gamma=0.01, vega=0.2, theta=-0.05, undPrice=450.0)


def _fake_ticker(
    contract,
    bid,
    ask,
    iv=0.2,
    delta=0.5,
    *,
    market_data_type=1,
    observed_at=None,
):
    observed_at = observed_at or datetime(2026, 7, 24, 20, tzinfo=timezone.utc)
    return SimpleNamespace(
        contract=contract,
        bid=bid,
        ask=ask,
        modelGreeks=_fake_greeks(iv, delta),
        lastGreeks=None,
        bidGreeks=None,
        askGreeks=None,
        impliedVolatility=iv,
        time=observed_at,
        marketDataType=market_data_type,
        lastTimestamp=observed_at if market_data_type == 2 else None,
        delayedLastTimestamp=(
            observed_at if market_data_type in {3, 4} else None
        ),
    )


class _FakeSleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


class _FakeIbSnapshot:
    """Echoes back a qualified contract per input (assigning a fake conId) and
    a ticker per qualified contract; records batch sizes to verify pacing."""

    def __init__(self):
        self.qualify_batches: list[int] = []
        self.ticker_batches: list[int] = []
        self.market_data_types: list[int] = []
        self._next_con_id = 1000

    def reqMarketDataType(self, t):
        self.market_data_types.append(t)

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_batches.append(len(contracts))
        out = []
        for c in contracts:
            self._next_con_id += 1
            out.append(
                _fake_contract(c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right, self._next_con_id)
            )
        return out

    async def reqTickersAsync(self, *contracts):
        self.ticker_batches.append(len(contracts))
        return [_fake_ticker(c, bid=1.0, ask=1.2) for c in contracts]


def _params(expirations=("20260918",), strikes=(440.0, 450.0)):
    return OptionChainParams(
        underlying_symbol="SPY",
        underlying_con_id=756733,
        trading_class="SPY",
        exchange="SMART",
        multiplier="100",
        expirations=expirations,
        strikes=strikes,
    )


async def test_snapshot_option_quotes_builds_calls_and_puts_per_strike_expiry():
    ib = _FakeIbSnapshot()
    sleeper = _FakeSleeper()
    chain = _params(expirations=("20260918",), strikes=(440.0, 450.0))
    quotes = await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=sleeper, pace_seconds=0.3, batch_size=50
    )
    # 1 expiry x 2 strikes x 2 rights = 4 contracts
    assert len(quotes) == 4
    rights = sorted((q.expiry, q.strike, q.right) for q in quotes)
    assert rights == [
        ("20260918", 440.0, "C"),
        ("20260918", 440.0, "P"),
        ("20260918", 450.0, "C"),
        ("20260918", 450.0, "P"),
    ]
    q = quotes[0]
    assert q.bid == pytest.approx(1.0)
    assert q.ask == pytest.approx(1.2)
    assert q.iv == pytest.approx(0.2)
    assert q.delta == pytest.approx(0.5)
    assert q.multiplier == pytest.approx(100.0)
    assert q.con_id is not None


async def test_snapshot_option_quotes_paces_between_batches():
    ib = _FakeIbSnapshot()
    sleeper = _FakeSleeper()
    chain = _params(expirations=("20260918", "20261016"), strikes=(440.0, 450.0, 460.0))
    # 2 expiries x 3 strikes x 2 rights = 12 contracts, batch_size=5 -> 3 batches
    await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=sleeper, pace_seconds=0.25, batch_size=5
    )
    assert ib.qualify_batches == [5, 5, 2]
    assert ib.ticker_batches == [5, 5, 2]
    assert sleeper.delays == [0.25, 0.25, 0.25]


async def test_snapshot_option_quotes_skips_unresolvable_contracts_without_raising():
    class _PartialFailIb(_FakeIbSnapshot):
        async def qualifyContractsAsync(self, *contracts):
            self.qualify_batches.append(len(contracts))
            # first contract fails to resolve (IB returns None in that slot)
            out = [None]
            for c in contracts[1:]:
                self._next_con_id += 1
                out.append(
                    _fake_contract(c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right, self._next_con_id)
                )
            return out

    ib = _PartialFailIb()
    chain = _params(expirations=("20260918",), strikes=(440.0,))
    quotes = await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=_FakeSleeper(), pace_seconds=0.1, batch_size=50
    )
    assert len(quotes) == 1  # the 2 contracts requested (C,P) minus 1 unresolvable


async def test_snapshot_option_quotes_treats_sentinel_bid_ask_as_missing():
    class _SentinelIb(_FakeIbSnapshot):
        async def reqTickersAsync(self, *contracts):
            self.ticker_batches.append(len(contracts))
            return [_fake_ticker(c, bid=-1.0, ask=-1.0, iv=-1.0, delta=None) for c in contracts]

    ib = _SentinelIb()
    chain = _params(expirations=("20260918",), strikes=(440.0,))
    quotes = await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=_FakeSleeper(), pace_seconds=0.1, batch_size=50
    )
    assert all(q.bid is None and q.ask is None and q.iv is None and q.delta is None for q in quotes)


async def test_snapshot_requests_delayed_frozen_market_data_by_default():
    # Error 354 field report (2026-07-26): live OPRA snapshots are rejected on
    # the paper session ("Delayed market data is available"). The chain cache
    # feeds daily risk math, so delayed-frozen (type 4) is the honest default;
    # operators with live sharing enabled can pass market_data_type=1.
    ib = _FakeIbSnapshot()
    sleeper = _FakeSleeper()
    chain = _params()
    await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=sleeper
    )
    assert ib.market_data_types == [4]


async def test_snapshot_market_data_type_is_overridable():
    ib = _FakeIbSnapshot()
    sleeper = _FakeSleeper()
    chain = _params()
    await snapshot_option_quotes(
        ib, chain, expiries=chain.expirations, strikes=chain.strikes, sleep=sleeper, market_data_type=1
    )
    assert ib.market_data_types == [1]


async def test_snapshot_delayed_frozen_quote_preserves_market_time_and_type():
    observed_at = datetime(2026, 7, 23, 20, 15, tzinfo=timezone.utc)

    class _DelayedFrozenIb(_FakeIbSnapshot):
        async def reqTickersAsync(self, *contracts):
            self.ticker_batches.append(len(contracts))
            return [
                _fake_ticker(
                    contract,
                    bid=1.0,
                    ask=1.2,
                    market_data_type=4,
                    observed_at=observed_at,
                )
                for contract in contracts
            ]

    chain = _params(expirations=("20260918",), strikes=(440.0,))
    quotes = await snapshot_option_quotes(
        _DelayedFrozenIb(),
        chain,
        expiries=chain.expirations,
        strikes=chain.strikes,
        sleep=_FakeSleeper(),
    )

    assert {quote.market_data_type for quote in quotes} == {4}
    assert {quote.observed_at for quote in quotes} == {
        "2026-07-23T20:15:00Z"
    }


async def test_snapshot_drops_delayed_frozen_quote_without_market_timestamp():
    class _UnstampedDelayedFrozenIb(_FakeIbSnapshot):
        async def reqTickersAsync(self, *contracts):
            self.ticker_batches.append(len(contracts))
            tickers = [
                _fake_ticker(
                    contract,
                    bid=1.0,
                    ask=1.2,
                    market_data_type=4,
                )
                for contract in contracts
            ]
            for ticker in tickers:
                ticker.delayedLastTimestamp = None
            return tickers

    chain = _params(expirations=("20260918",), strikes=(440.0,))

    quotes = await snapshot_option_quotes(
        _UnstampedDelayedFrozenIb(),
        chain,
        expiries=chain.expirations,
        strikes=chain.strikes,
        sleep=_FakeSleeper(),
    )

    assert quotes == []


async def test_snapshot_held_option_rejects_qualified_contract_substitution():
    class _SubstitutingIb(_FakeIbSnapshot):
        async def qualifyContractsAsync(self, *contracts):
            self.qualify_batches.append(len(contracts))
            requested = contracts[0]
            return [
                _fake_contract(
                    requested.symbol,
                    requested.lastTradeDateOrContractMonth,
                    requested.strike,
                    requested.right,
                    con_id=999999,
                )
            ]

    position = SimpleNamespace(
        con_id=123456,
        symbol="SPY",
        qty=1,
        sec_type="OPT",
        multiplier=100,
        strike=440.0,
        expiry="20260918",
        right="C",
        exchange="SMART",
        currency="USD",
    )
    ib = _SubstitutingIb()

    quotes = await snapshot_held_option_quotes(
        ib,
        [position],
        sleep=_FakeSleeper(),
        pace_seconds=0.1,
    )

    assert quotes == []
    assert ib.ticker_batches == []


async def test_snapshot_held_option_rejects_ticker_contract_substitution():
    class _TickerSubstitutingIb(_FakeIbSnapshot):
        async def qualifyContractsAsync(self, *contracts):
            self.qualify_batches.append(len(contracts))
            return list(contracts)

        async def reqTickersAsync(self, *contracts):
            self.ticker_batches.append(len(contracts))
            requested = contracts[0]
            substituted = _fake_contract(
                requested.symbol,
                requested.lastTradeDateOrContractMonth,
                requested.strike,
                requested.right,
                con_id=999999,
            )
            return [_fake_ticker(substituted, bid=1.0, ask=1.2)]

    position = SimpleNamespace(
        con_id=123456,
        symbol="SPY",
        qty=1,
        sec_type="OPT",
        multiplier=100,
        strike=440.0,
        expiry="20260918",
        right="C",
        exchange="SMART",
        currency="USD",
    )

    quotes = await snapshot_held_option_quotes(
        _TickerSubstitutingIb(),
        [position],
        sleep=_FakeSleeper(),
        pace_seconds=0.1,
    )

    assert quotes == []
