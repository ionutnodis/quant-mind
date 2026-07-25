import numpy as np
import pandas as pd
import pytest

from quantmind.datastore.store import BarStore
from quantmind.sources.sync import (
    merge_bars,
    sync_daily_bars,
    sync_index_bars,
    sync_instrument_metadata,
    sync_yfinance_bars,
    yfinance_pseudo_con_id,
)


def _bars(start, n, price0=100.0):
    idx = pd.bdate_range(start, periods=n)
    close = price0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_merge_bars_new_wins_on_overlap_union_sorted():
    old = _bars("2026-01-05", 5, price0=100.0)
    new = _bars("2026-01-08", 5, price0=500.0)  # overlaps last 2 days of old
    merged = merge_bars(old, new)
    assert merged.index.is_monotonic_increasing
    assert len(merged) == 8  # 3 old-only + 5 new
    assert merged.loc["2026-01-08", "close"] == 500.0  # new data wins on overlap
    assert merged.loc["2026-01-05", "close"] == 100.0  # old preserved before overlap


class FakeBroker:
    def __init__(self):
        self.con_ids = {"SPY": 756733, "QQQ": 320227571}
        self.bar_calls = []  # (con_id, years)

    async def resolve_stock_con_id(self, symbol):
        return self.con_ids[symbol]

    async def get_daily_bars(self, con_id, years=5):
        self.bar_calls.append((con_id, years))
        return _bars("2026-01-05", 250)


class FakeSleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


async def test_first_sync_fetches_full_history_and_saves_symbol_map(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeBroker(), FakeSleeper()
    symbol_map = await sync_daily_bars(
        store, broker, ["SPY", "QQQ"], years=5, sleep=sleeper, pace_seconds=0.5
    )
    assert symbol_map == {"SPY": 756733, "QQQ": 320227571}
    assert store.read_symbol_map() == symbol_map
    assert all(years == 5 for _, years in broker.bar_calls)
    bars, meta = store.read_bars(con_id=756733, bar_size="1d")
    assert len(bars) == 250
    assert meta.bar_type == "ADJUSTED_LAST"
    # pacing between instruments (Engineering Constraint 6)
    assert sleeper.delays == [0.5, 0.5]


async def test_incremental_sync_fetches_short_duration_and_merges(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeBroker(), FakeSleeper()
    await sync_daily_bars(store, broker, ["SPY"], years=5, sleep=sleeper)
    broker.bar_calls.clear()
    await sync_daily_bars(store, broker, ["SPY"], years=5, sleep=sleeper)
    # watermark exists -> incremental fetch requests far less than full history
    assert broker.bar_calls[0][1] == 1
    bars, _ = store.read_bars(con_id=756733, bar_size="1d")
    assert len(bars) == 250  # merge did not duplicate overlapping dates


def test_symbol_map_round_trip(tmp_path):
    store = BarStore(tmp_path)
    assert store.read_symbol_map() == {}
    store.write_symbol_map({"SPY": 1, "GLD": 2})
    assert store.read_symbol_map() == {"SPY": 1, "GLD": 2}


# --- Task A2: index sync, instrument-metadata sync, yfinance fallback sync ---


class FakeIndexBroker:
    """Fake for sync_index_bars: resolve_index_con_id + get_index_bars."""

    def __init__(self):
        self.con_ids = {"VIX": 13455763, "SPX": 416904}
        self.bar_calls = []  # (con_id, exchange, years)

    async def resolve_index_con_id(self, symbol, exchange):
        return self.con_ids[symbol]

    async def get_index_bars(self, con_id, exchange, years=5):
        self.bar_calls.append((con_id, exchange, years))
        return _bars("2026-01-05", 250, price0=15.0)


async def test_sync_index_bars_resolves_and_caches_vix_spx(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeIndexBroker(), FakeSleeper()
    symbol_map = await sync_index_bars(
        store, broker, {"VIX": "CBOE", "SPX": "CBOE"}, years=5, sleep=sleeper, pace_seconds=0.25
    )
    assert symbol_map == {"VIX": 13455763, "SPX": 416904}
    assert store.read_symbol_map() == symbol_map
    bars, meta = store.read_bars(con_id=13455763, bar_size="1d")
    assert len(bars) == 250
    assert meta.bar_type == "TRADES"  # indices: TRADES, not ADJUSTED_LAST
    assert sleeper.delays == [0.25, 0.25]


async def test_sync_index_bars_merges_into_existing_symbol_map(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeIndexBroker(), FakeSleeper()
    store.write_symbol_map({"SPY": 756733})  # from a prior sync_daily_bars call
    await sync_index_bars(store, broker, {"VIX": "CBOE"}, sleep=sleeper)
    assert store.read_symbol_map() == {"SPY": 756733, "VIX": 13455763}


async def test_sync_index_bars_incremental_fetches_short_duration(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeIndexBroker(), FakeSleeper()
    await sync_index_bars(store, broker, {"VIX": "CBOE"}, sleep=sleeper)
    broker.bar_calls.clear()
    await sync_index_bars(store, broker, {"VIX": "CBOE"}, sleep=sleeper)
    assert broker.bar_calls[0][2] == 1  # years=1, watermark exists


class FakeMetadataBroker:
    def __init__(self):
        self.details = {
            756733: {
                "long_name": "SPDR S&P 500",
                "exchange": "ARCA",
                "currency": "USD",
                "sec_type": "STK",
                "industry": None,
            },
            13455763: {
                "long_name": "CBOE Volatility Index",
                "exchange": "CBOE",
                "currency": "USD",
                "sec_type": "IND",
                "industry": None,
            },
        }
        self.calls = []

    async def fetch_contract_details(self, con_id):
        self.calls.append(con_id)
        return self.details[con_id]


async def test_sync_instrument_metadata_writes_contract_details(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    written = await sync_instrument_metadata(
        store, broker, {"SPY": 756733, "VIX": 13455763}, sleep=sleeper, pace_seconds=0.1
    )
    assert written["SPY"]["long_name"] == "SPDR S&P 500"
    assert written["SPY"]["provider"] == "ibkr"
    assert written["SPY"]["con_id"] == 756733
    got = store.read_instrument_metadata("VIX")
    assert got["exchange"] == "CBOE"
    assert got["provider"] == "ibkr"
    assert sleeper.delays == [0.1, 0.1]


async def test_sync_instrument_metadata_merges_extra_tags(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    await sync_instrument_metadata(
        store, broker, {"SPY": 756733}, extra_tags={"SPY": {"region": "US"}}, sleep=sleeper
    )
    got = store.read_instrument_metadata("SPY")
    assert got["region"] == "US"
    assert got["long_name"] == "SPDR S&P 500"


def test_yfinance_pseudo_con_id_is_negative_deterministic_and_distinct():
    a = yfinance_pseudo_con_id("EZU")
    b = yfinance_pseudo_con_id("EZU")
    c = yfinance_pseudo_con_id("EWU")
    assert a == b
    assert a < 0
    assert a != c


class FakeYFinanceProvider:
    name = "yfinance"

    def __init__(self):
        self.calls = []

    def daily_bars(self, symbol):
        self.calls.append(symbol)
        return _bars("2026-01-05", 300, price0=50.0)


def test_sync_yfinance_bars_writes_bars_and_provider_metadata(tmp_path):
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    symbol_map = sync_yfinance_bars(store, provider, ["EZU", "EWU"], years=1)
    ezu_con_id = yfinance_pseudo_con_id("EZU")
    assert symbol_map["EZU"] == ezu_con_id
    assert symbol_map["EWU"] == yfinance_pseudo_con_id("EWU")
    bars, meta = store.read_bars(con_id=ezu_con_id, bar_size="1d")
    assert len(bars) == 252  # trimmed to years=1
    assert meta.bar_type == "ADJUSTED_LAST"
    got_meta = store.read_instrument_metadata("EZU")
    assert got_meta["provider"] == "yfinance"
    assert got_meta["con_id"] == ezu_con_id
    assert store.read_symbol_map()["EZU"] == ezu_con_id


def test_sync_yfinance_bars_merges_into_existing_symbol_map(tmp_path):
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    store.write_symbol_map({"SPY": 756733})
    sync_yfinance_bars(store, provider, ["EZU"])
    assert store.read_symbol_map()["SPY"] == 756733
    assert "EZU" in store.read_symbol_map()
