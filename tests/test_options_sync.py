"""sync_options_chains: composes ib_options.py (chain params + paced
snapshot) + OptionsStore for configured underliers, spot sourced from the
already-synced stock bars (BarStore) rather than a live snapshot (SPY/QQQ are
already in sync_cli.DEFAULT_UNIVERSE, so their bars are cached before this
ever runs). Tested with fakes only (pattern: tests/test_sync.py's FakeBroker).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quantmind.datastore.options_store import OptionsStore
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.portfolio import Position
from quantmind.sources.options_sync import sync_options_chains


def _bars(n=10, price=452.0):
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def bar_store(tmp_path):
    store = BarStore(tmp_path / "bars_root")
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=756733, bar_size="1d", bars=_bars(price=452.0), meta=meta)
    store.write_bars(con_id=320227571, bar_size="1d", bars=_bars(price=380.0), meta=meta)
    store.write_symbol_map({"SPY": 756733, "QQQ": 320227571})
    return store


@pytest.fixture
def options_store(tmp_path):
    return OptionsStore(tmp_path / "options_root")


def _fake_chain(underlier, con_id):
    return SimpleNamespace(
        exchange="SMART",
        underlyingConId=con_id,
        tradingClass=underlier,
        multiplier="100",
        expirations=["20260821"],  # third Friday of Aug 2026 -> a monthly, within 90d of 2026-07-24
        strikes=[380.0, 400.0, 420.0, 440.0, 452.0, 460.0, 480.0, 500.0, 520.0],
    )


def _fake_greeks(iv=0.2, delta=0.5):
    return SimpleNamespace(impliedVol=iv, delta=delta, gamma=0.01, vega=0.2, theta=-0.05, undPrice=452.0)


def _fake_contract(
    symbol,
    expiry,
    strike,
    right,
    con_id,
    *,
    multiplier="100",
    trading_class="",
    currency="",
    exchange="SMART",
):
    return SimpleNamespace(
        symbol=symbol, lastTradeDateOrContractMonth=expiry, strike=strike, right=right,
        conId=con_id, multiplier=multiplier, tradingClass=trading_class,
        currency=currency, exchange=exchange, secType="OPT",
    )


def _fake_ticker(contract):
    observed_at = datetime(2026, 7, 24, 20, 30, tzinfo=timezone.utc)
    return SimpleNamespace(
        contract=contract, bid=1.0, ask=1.2, modelGreeks=_fake_greeks(), lastGreeks=None,
        bidGreeks=None, askGreeks=None, impliedVolatility=0.2,
        time=observed_at, marketDataType=1, lastTimestamp=None,
        delayedLastTimestamp=None,
    )


class FakeIb:
    def __init__(self):
        self.sec_def_calls: list[tuple] = []
        self._next_con_id = 5000

    async def reqSecDefOptParamsAsync(self, symbol, exchange, sec_type, con_id):
        self.sec_def_calls.append((symbol, exchange, sec_type, con_id))
        return [_fake_chain(symbol, con_id)]

    async def qualifyContractsAsync(self, *contracts):
        out = []
        for c in contracts:
            con_id = getattr(c, "conId", 0)
            if not con_id:
                self._next_con_id += 1
                con_id = self._next_con_id
            out.append(
                _fake_contract(
                    c.symbol,
                    c.lastTradeDateOrContractMonth,
                    c.strike,
                    c.right,
                    con_id,
                    multiplier=c.multiplier,
                    trading_class=c.tradingClass,
                    currency=c.currency,
                    exchange=c.exchange,
                )
            )
        return out

    async def reqTickersAsync(self, *contracts):
        return [_fake_ticker(c) for c in contracts]


class EmptyTickerIb(FakeIb):
    async def reqTickersAsync(self, *contracts):
        return []


class DelayedTickerIb(FakeIb):
    async def reqTickersAsync(self, *contracts):
        tickers = [_fake_ticker(contract) for contract in contracts]
        for ticker in tickers:
            ticker.marketDataType = 4
            ticker.delayedLastTimestamp = datetime(
                2026,
                7,
                23,
                20,
                15,
                tzinfo=timezone.utc,
            )
        return tickers


class FakeSleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


async def test_sync_writes_a_chain_per_underlier(bar_store, options_store):
    ib = FakeIb()
    sleeper = FakeSleeper()
    counts = await sync_options_chains(
        options_store, bar_store, ib, underliers=["SPY", "QQQ"], as_of=date(2026, 7, 24),
        sleep=sleeper, pace_seconds=0.2,
    )
    assert set(counts) == {"SPY", "QQQ"}
    assert counts["SPY"] > 0

    df, meta = options_store.read_chain("SPY")
    assert meta.spot == pytest.approx(452.0)
    assert meta.as_of == "2026-07-24T20:30:00Z"
    assert meta.underlier_con_id == 756733
    # strikes within +/-15% of 452 (=[384.2, 519.8]) x 1 monthly expiry x {C,P}
    assert set(df["strike"]) <= {400.0, 420.0, 440.0, 452.0, 460.0, 480.0, 500.0}
    assert set(df["right"]) == {"C", "P"}
    assert set(df["observed_at"]) == {"2026-07-24T20:30:00Z"}
    assert set(df["market_data_type"]) == {1}


async def test_zero_quote_refresh_preserves_prior_option_evidence(
    bar_store,
    options_store,
):
    await sync_options_chains(
        options_store,
        bar_store,
        FakeIb(),
        underliers=["SPY"],
        as_of=date(2026, 7, 24),
        sleep=FakeSleeper(),
        pace_seconds=0.1,
    )
    prior_quotes, prior_meta = options_store.read_chain("SPY")

    counts = await sync_options_chains(
        options_store,
        bar_store,
        EmptyTickerIb(),
        underliers=["SPY"],
        as_of=date(2026, 7, 25),
        sleep=FakeSleeper(),
        pace_seconds=0.1,
    )

    assert counts == {"SPY": 0}
    stored_quotes, stored_meta = options_store.read_chain("SPY")
    pd.testing.assert_frame_equal(stored_quotes, prior_quotes)
    assert stored_meta == prior_meta


async def test_sync_snapshot_as_of_is_weakest_market_observation(
    bar_store,
    options_store,
):
    await sync_options_chains(
        options_store,
        bar_store,
        DelayedTickerIb(),
        underliers=["SPY"],
        as_of=date(2026, 7, 24),
        sleep=FakeSleeper(),
        pace_seconds=0.1,
    )

    quotes, meta = options_store.read_chain("SPY")
    assert meta.as_of == "2026-07-23T20:15:00Z"
    assert set(quotes["observed_at"]) == {meta.as_of}
    assert set(quotes["market_data_type"]) == {4}


async def test_sync_uses_last_close_as_spot_not_a_live_call(bar_store, options_store):
    ib = FakeIb()
    await sync_options_chains(
        options_store, bar_store, ib, underliers=["QQQ"], as_of=date(2026, 7, 24),
        sleep=FakeSleeper(), pace_seconds=0.1,
    )
    _, meta = options_store.read_chain("QQQ")
    assert meta.spot == pytest.approx(380.0)


async def test_sync_paces_between_underliers(bar_store, options_store):
    sleeper = FakeSleeper()
    await sync_options_chains(
        options_store, bar_store, FakeIb(), underliers=["SPY", "QQQ"], as_of=date(2026, 7, 24),
        sleep=sleeper, pace_seconds=0.4,
    )
    assert 0.4 in sleeper.delays


async def test_sync_raises_lookup_error_for_underlier_missing_from_symbol_map(bar_store, options_store):
    with pytest.raises(LookupError):
        await sync_options_chains(
            options_store, bar_store, FakeIb(), underliers=["MSFT"], as_of=date(2026, 7, 24),
            sleep=FakeSleeper(),
        )


async def test_sync_includes_an_exact_held_contract_outside_the_surface_sample(
    bar_store, options_store
):
    held = Position(
        con_id=987654,
        symbol="SPY",
        qty=-2,
        sec_type="OPT",
        multiplier=100,
        strike=800.0,
        expiry="20271217",
        right="C",
    )

    await sync_options_chains(
        options_store,
        bar_store,
        FakeIb(),
        underliers=["SPY"],
        held_contracts=[held],
        as_of=date(2026, 7, 24),
        sleep=FakeSleeper(),
        pace_seconds=0.1,
    )

    frame, _ = options_store.read_chain("SPY")
    held_rows = frame[
        (frame["expiry"] == "20271217")
        & (frame["strike"] == 800.0)
        & (frame["right"] == "C")
    ]
    assert len(held_rows) == 1


async def test_sync_preserves_distinct_contract_ids_with_identical_terms(
    bar_store, options_store
):
    held = Position(
        con_id=987654,
        symbol="SPY",
        qty=1,
        sec_type="OPT",
        multiplier=100,
        strike=452.0,
        expiry="20260821",
        right="C",
    )

    await sync_options_chains(
        options_store,
        bar_store,
        FakeIb(),
        underliers=["SPY"],
        held_contracts=[held],
        as_of=date(2026, 7, 24),
        sleep=FakeSleeper(),
        pace_seconds=0.1,
    )

    frame, _ = options_store.read_chain("SPY")
    matching = frame[
        (frame["expiry"] == "20260821")
        & (frame["strike"] == 452.0)
        & (frame["right"] == "C")
    ]
    assert len(matching) == 2
    assert matching["con_id"].nunique() == 2
