import numpy as np
import pandas as pd
import pytest

from quantmind.datastore.store import BarStore
from quantmind.sources.sync import merge_bars, sync_daily_bars


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
