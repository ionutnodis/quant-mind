import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.api.routers.setup import _market_data_status
from quantmind.datastore.store import BarMeta, BarStore
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
        self.resolve_calls = []

    async def resolve_stock_con_id(self, symbol):
        self.resolve_calls.append(symbol)
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


async def test_sync_uses_authoritative_held_stock_con_id_without_symbol_resolution(tmp_path):
    store = BarStore(tmp_path)
    broker = FakeBroker()
    sleeper = FakeSleeper()

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["DUAL"],
        years=5,
        sleep=sleeper,
        known_con_ids={"DUAL": 424242},
    )

    assert symbol_map == {"DUAL": 424242}
    assert broker.resolve_calls == []
    assert all(years == 5 for _, years in broker.bar_calls)
    bars, meta = store.read_bars(con_id=424242, bar_size="1d")
    assert len(bars) == 250
    assert meta.bar_type == "ADJUSTED_LAST"
    assert sleeper.delays == [0.5]


class OptionAwareBroker(FakeBroker):
    def __init__(self, option_underliers: dict[int, int]):
        super().__init__()
        self.option_underliers = option_underliers
        self.option_resolve_calls: list[int] = []

    async def resolve_option_underlying_con_id(self, option_con_id):
        self.option_resolve_calls.append(option_con_id)
        if option_con_id not in self.option_underliers:
            raise LookupError(f"no underlying for option {option_con_id}")
        return self.option_underliers[option_con_id]


async def test_option_only_underlier_uses_held_contract_identity_not_usd_ticker_lookup(
    tmp_path,
):
    store = BarStore(tmp_path)
    broker = OptionAwareBroker({7001: 12345})
    broker.con_ids["ASML"] = 99999  # the wrong same-ticker USD listing
    sleeper = FakeSleeper()

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["ASML"],
        sleep=sleeper,
        option_contract_con_ids={"ASML": [7001]},
    )

    assert symbol_map == {"ASML": 12345}
    assert broker.option_resolve_calls == [7001]
    assert broker.resolve_calls == []
    assert broker.bar_calls == [(12345, 5)]
    assert sleeper.delays == [0.5]


async def test_failed_option_underlier_resolution_never_falls_back_and_isolates_next_symbol(
    tmp_path,
):
    store = BarStore(tmp_path)
    broker = OptionAwareBroker({})
    broker.con_ids["ASML"] = 99999  # must never be selected after authoritative failure
    sleeper = FakeSleeper()
    failures: dict[str, str] = {}

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["ASML", "SPY"],
        sleep=sleeper,
        pace_seconds=0.25,
        option_contract_con_ids={"ASML": [7001]},
        failures=failures,
    )

    assert symbol_map == {"SPY": 756733}
    assert "ASML" in failures
    assert broker.option_resolve_calls == [7001]
    assert broker.resolve_calls == ["SPY"]
    assert broker.bar_calls == [(756733, 5)]
    assert sleeper.delays == [0.25, 0.25]


async def test_conflicting_option_underliers_for_one_symbol_fail_closed(tmp_path):
    store = BarStore(tmp_path)
    broker = OptionAwareBroker({7001: 12345, 7002: 99999})
    failures: dict[str, str] = {}

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["ASML"],
        sleep=FakeSleeper(),
        option_contract_con_ids={"ASML": [7001, 7002]},
        failures=failures,
    )

    assert symbol_map == {}
    assert "conflicting authoritative underlying conIds" in failures["ASML"]
    assert broker.resolve_calls == []
    assert broker.bar_calls == []


async def test_held_stock_and_option_underlier_identity_conflict_fails_closed(tmp_path):
    store = BarStore(tmp_path)
    broker = OptionAwareBroker({7001: 12345})
    failures: dict[str, str] = {}

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["ASML"],
        sleep=FakeSleeper(),
        known_con_ids={"ASML": 99999},
        option_contract_con_ids={"ASML": [7001]},
        failures=failures,
    )

    assert symbol_map == {}
    assert "conflicting authoritative underlying conIds" in failures["ASML"]
    assert broker.resolve_calls == []
    assert broker.bar_calls == []


async def test_multiple_held_stock_listings_for_one_symbol_fail_without_blocking_others(
    tmp_path,
):
    store = BarStore(tmp_path)
    store.write_symbol_map({"ASML": 77777})
    broker = FakeBroker()
    sleeper = FakeSleeper()
    failures: dict[str, str] = {}

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["ASML", "SPY"],
        sleep=sleeper,
        pace_seconds=0.25,
        known_con_ids={"ASML": [12345, 99999]},
        failures=failures,
    )

    assert symbol_map == {"SPY": 756733}
    assert "conflicting authoritative underlying conIds" in failures["ASML"]
    assert "ASML" not in store.read_symbol_map()
    assert broker.resolve_calls == ["SPY"]
    assert broker.bar_calls == [(756733, 5)]
    assert sleeper.delays == [0.25, 0.25]


async def test_identity_conflict_removes_stale_mapping_and_blocks_setup(tmp_path):
    store = BarStore(tmp_path)
    today = pd.Timestamp.now().normalize()
    bars = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1_000.0],
        },
        index=pd.DatetimeIndex([today]),
    )
    store.write_bars(
        77777,
        "1d",
        bars,
        BarMeta(
            bar_type="ADJUSTED_LAST",
            adjusted_asof=today.date().isoformat(),
        ),
    )
    store.write_symbol_map({"ASML": 77777})
    store.write_required_symbols(["ASML"])
    failures: dict[str, str] = {}

    await sync_daily_bars(
        store,
        FakeBroker(),
        ["ASML"],
        sleep=FakeSleeper(),
        known_con_ids={"ASML": [12345, 99999]},
        failures=failures,
    )

    assert "ASML" not in store.read_symbol_map()
    assert "InstrumentIdentityConflictError" in failures["ASML"]
    readiness = _market_data_status(store, "ASML")
    assert readiness.status != "ready"
    assert readiness.missing_symbols == ["ASML"]
    client = TestClient(
        create_app(store=store, benchmark="ASML"),
        base_url="http://127.0.0.1",
    )
    response = client.get("/api/risk/ASML")
    assert response.status_code == 422
    assert "not in cache" in response.json()["detail"]


async def test_partial_universe_sync_preserves_existing_symbol_map_entries(tmp_path):
    # Fix-loop IMPORTANT 1: `python -m quantmind.sync_cli SPY` (documented
    # argv mode) syncs a subset — sync_daily_bars must read-modify-write the
    # symbol map, not rebuild it from {}, or every other mapping (VIX/SPX,
    # world ETFs, yfinance entries) is silently wiped and /api/instruments/*
    # breaks for all of them.
    store, broker, sleeper = BarStore(tmp_path), FakeBroker(), FakeSleeper()
    store.write_symbol_map({"VIX": 13455763, "EZU": -12345})
    symbol_map = await sync_daily_bars(store, broker, ["SPY"], years=5, sleep=sleeper)
    assert symbol_map["SPY"] == 756733
    persisted = store.read_symbol_map()
    assert persisted["VIX"] == 13455763  # pre-existing index mapping survives
    assert persisted["EZU"] == -12345  # pre-existing yfinance mapping survives
    assert persisted["SPY"] == 756733


async def test_daily_sync_isolates_one_symbol_failure_and_publishes_successes(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeBroker(), FakeSleeper()
    failures: dict[str, str] = {}

    symbol_map = await sync_daily_bars(
        store,
        broker,
        ["SPY", "BAD", "QQQ"],
        sleep=sleeper,
        pace_seconds=0,
        failures=failures,
    )

    assert symbol_map == {"SPY": 756733, "QQQ": 320227571}
    assert store.read_symbol_map() == symbol_map
    assert "BAD" in failures
    assert store.watermark(756733, "1d") is not None
    assert store.watermark(320227571, "1d") is not None


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


async def test_index_sync_isolates_one_failure_and_publishes_later_symbols(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeIndexBroker(), FakeSleeper()
    store.write_symbol_map({"SPY": 756733})
    failures: dict[str, str] = {}

    symbol_map = await sync_index_bars(
        store,
        broker,
        {"VIX": "CBOE", "BAD": "CBOE", "SPX": "CBOE"},
        sleep=sleeper,
        pace_seconds=0,
        failures=failures,
    )

    assert symbol_map == {"VIX": 13455763, "SPX": 416904}
    assert store.read_symbol_map() == {
        "SPY": 756733,
        "VIX": 13455763,
        "SPX": 416904,
    }
    assert "BAD" in failures
    assert store.watermark(416904, "1d") is not None


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


async def test_sync_instrument_metadata_atomically_rebuilds_a_corrupt_cache(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    path = tmp_path / "instruments.json"
    path.write_text('{"POISONED": 7}')

    written = await sync_instrument_metadata(
        store,
        broker,
        {"SPY": 756733, "VIX": 13455763},
        sleep=sleeper,
        pace_seconds=0,
    )

    assert set(written) == {"SPY", "VIX"}
    rebuilt = store.read_all_instrument_metadata()
    assert set(rebuilt) == {"SPY", "VIX"}
    assert rebuilt["SPY"]["currency"] == "USD"
    assert rebuilt["VIX"]["exchange"] == "CBOE"
    assert not path.with_suffix(".json.tmp").exists()


async def test_corrupt_metadata_rebuild_salvages_valid_records_during_partial_failure(
    tmp_path,
):
    import json

    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    path = tmp_path / "instruments.json"
    path.write_text(
        json.dumps(
            {
                "UNTOUCHED": {
                    "con_id": 42,
                    "currency": "EUR",
                    "provider": "yfinance",
                },
                "FAILED": {
                    "con_id": 999,
                    "currency": "GBP",
                    "provider": "ibkr",
                },
                "POISON": 7,
            }
        )
    )
    failures: dict[str, str] = {}

    written = await sync_instrument_metadata(
        store,
        broker,
        {"SPY": 756733, "FAILED": 999},
        sleep=sleeper,
        pace_seconds=0,
        failures=failures,
    )

    assert set(written) == {"SPY"}
    assert "FAILED" in failures
    rebuilt = store.read_all_instrument_metadata()
    assert set(rebuilt) == {"UNTOUCHED", "FAILED", "SPY"}
    assert rebuilt["UNTOUCHED"]["con_id"] == 42
    assert rebuilt["FAILED"]["currency"] == "GBP"
    assert rebuilt["SPY"]["con_id"] == 756733


async def test_sync_instrument_metadata_merges_extra_tags(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    await sync_instrument_metadata(
        store, broker, {"SPY": 756733}, extra_tags={"SPY": {"region": "US"}}, sleep=sleeper
    )
    got = store.read_instrument_metadata("SPY")
    assert got["region"] == "US"
    assert got["long_name"] == "SPDR S&P 500"


async def test_metadata_sync_isolates_failure_and_preserves_failed_provenance(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    prior = {"con_id": 999, "provider": "legacy", "currency": "EUR"}
    store.write_instrument_metadata("BAD", prior)
    failures: dict[str, str] = {}

    written = await sync_instrument_metadata(
        store,
        broker,
        {"SPY": 756733, "BAD": 999, "VIX": 13455763},
        sleep=sleeper,
        pace_seconds=0,
        failures=failures,
    )

    assert set(written) == {"SPY", "VIX"}
    assert "BAD" in failures
    assert store.read_instrument_metadata("BAD") == prior
    assert store.read_instrument_metadata("VIX")["provider"] == "ibkr"


async def test_ibkr_identity_refresh_clears_stale_ucits_linkage(tmp_path):
    store, broker, sleeper = BarStore(tmp_path), FakeMetadataBroker(), FakeSleeper()
    store.write_instrument_metadata(
        "SPY",
        {
            "con_id": 111,
            "provider": "ibkr",
            "region": "US",
            "isin": "IE00B4L5Y983",
            "stock_type": "ETF",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "FRESH",
            "ucits_profile_reason": "cached",
        },
    )

    await sync_instrument_metadata(
        store, broker, {"SPY": 756733}, sleep=sleeper, pace_seconds=0
    )

    metadata = store.read_instrument_metadata("SPY")
    assert metadata["con_id"] == 756733
    assert metadata["isin"] is None
    assert metadata["stock_type"] is None
    assert metadata["valid_exchanges"] == []
    assert metadata["external_identifiers"] == {}
    assert metadata["ucits_profile_isin"] is None
    assert metadata["ucits_profile_status"] is None
    assert metadata["region"] == "US"


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

    def quote_currency(self, symbol):
        return "EUR" if symbol in {"EZU", "EWU"} else "USD"

    def quote_convention(self, symbol):
        if symbol == "LGEN.L":
            return "GBP", "GBp", 0.01
        currency = self.quote_currency(symbol)
        return currency, currency, 1.0


def test_sync_yfinance_bars_writes_bars_and_provider_metadata(tmp_path):
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    symbol_map, skipped = sync_yfinance_bars(store, provider, ["EZU", "EWU"], years=1)
    ezu_con_id = yfinance_pseudo_con_id("EZU")
    assert skipped == []
    assert symbol_map["EZU"] == ezu_con_id
    assert symbol_map["EWU"] == yfinance_pseudo_con_id("EWU")
    bars, meta = store.read_bars(con_id=ezu_con_id, bar_size="1d")
    assert len(bars) == 252  # trimmed to years=1
    assert meta.bar_type == "ADJUSTED_LAST"
    got_meta = store.read_instrument_metadata("EZU")
    assert got_meta["provider"] == "yfinance"
    assert got_meta["con_id"] == ezu_con_id
    assert got_meta["currency"] == "EUR"
    assert got_meta["price_scale"] == 1.0
    assert store.read_symbol_map()["EZU"] == ezu_con_id


def test_sync_yfinance_bars_merges_into_existing_symbol_map(tmp_path):
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    store.write_symbol_map({"SPY": 756733})
    sync_yfinance_bars(store, provider, ["EZU"])
    assert store.read_symbol_map()["SPY"] == 756733
    assert "EZU" in store.read_symbol_map()


def test_sync_yfinance_london_pence_bars_are_normalized_to_gbp(tmp_path):
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()

    symbol_map, skipped = sync_yfinance_bars(store, provider, ["LGEN.L"], years=1)

    assert skipped == []
    bars, _meta = store.read_bars(symbol_map["LGEN.L"], "1d")
    assert bars["close"].iloc[0] == pytest.approx(0.98)
    metadata = store.read_instrument_metadata("LGEN.L")
    assert metadata["currency"] == "GBP"
    assert metadata["quote_unit"] == "GBp"
    assert metadata["price_scale"] == pytest.approx(0.01)


def test_sync_yfinance_bars_skips_ibkr_mapped_symbols_and_keeps_provenance(tmp_path):
    # Fix-loop IMPORTANT 2: an operator putting an IBKR-synced symbol on
    # QM_YFINANCE_SYMBOLS must NOT silently repoint its symbol_map entry to
    # the negative pseudo-conId / flip its provider — IBKR provenance wins
    # (single-provenance law). The symbol is skipped and reported.
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    store.write_symbol_map({"EEM": 2})  # positive conId = IBKR-sourced
    store.write_instrument_metadata("EEM", {"con_id": 2, "provider": "ibkr", "long_name": "iShares EM"})
    symbol_map, skipped = sync_yfinance_bars(store, provider, ["EEM", "EZU"])
    assert skipped == ["EEM"]
    assert "EEM" not in provider.calls  # no bars fetched for the skipped symbol
    assert symbol_map["EEM"] == 2  # mapping untouched
    assert store.read_symbol_map()["EEM"] == 2
    meta = store.read_instrument_metadata("EEM")
    assert meta["provider"] == "ibkr"  # provenance intact
    assert meta["con_id"] == 2
    # the non-conflicting symbol still syncs normally
    assert symbol_map["EZU"] == yfinance_pseudo_con_id("EZU")


def test_sync_yfinance_bars_resync_of_own_symbol_is_not_skipped(tmp_path):
    # A yfinance symbol re-appearing on the allowlist (its own negative
    # pseudo-conId already in the map) is a refresh, not a conflict.
    store, provider = BarStore(tmp_path), FakeYFinanceProvider()
    sync_yfinance_bars(store, provider, ["EZU"])
    provider.calls.clear()
    symbol_map, skipped = sync_yfinance_bars(store, provider, ["EZU"])
    assert skipped == []
    assert provider.calls == ["EZU"]
    assert symbol_map["EZU"] == yfinance_pseudo_con_id("EZU")


def test_yfinance_sync_isolates_failure_and_preserves_failed_provenance(tmp_path):
    class FailingMiddleProvider(FakeYFinanceProvider):
        def daily_bars(self, symbol):
            if symbol == "BAD":
                raise ValueError("bad vendor payload")
            return super().daily_bars(symbol)

    store, provider = BarStore(tmp_path), FailingMiddleProvider()
    prior_meta = {"con_id": -999, "provider": "legacy", "currency": "CHF"}
    store.write_symbol_map({"SPY": 756733, "BAD": -999})
    store.write_instrument_metadata("BAD", prior_meta)
    failures: dict[str, str] = {}

    symbol_map, skipped = sync_yfinance_bars(
        store,
        provider,
        ["EZU", "BAD", "EWU"],
        years=1,
        failures=failures,
    )

    assert skipped == []
    assert set(symbol_map) == {"EZU", "EWU"}
    assert "BAD" in failures
    assert store.read_symbol_map()["BAD"] == -999
    assert store.read_instrument_metadata("BAD") == prior_meta
    assert store.read_instrument_metadata("EWU")["provider"] == "yfinance"


def test_yfinance_fallback_retains_currency_and_enters_risk_analysis(tmp_path):
    store = BarStore(tmp_path)
    sync_yfinance_bars(store, FakeYFinanceProvider(), ["EZU"], years=1)
    client = TestClient(
        create_app(store=store, benchmark="EZU", base_currency="EUR"),
        base_url="http://127.0.0.1",
    )

    response = client.get("/api/risk/EZU", params={"window": 20, "years": 1})

    assert response.status_code == 200
    assert response.json()["symbol"] == "EZU"
    assert store.read_instrument_metadata("EZU")["currency"] == "EUR"
