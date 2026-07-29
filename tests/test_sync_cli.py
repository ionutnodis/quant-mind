"""Task A2: universe/config wiring in sync_cli — pure data, no I/O. The
async `main()` orchestration itself talks to a live IB Gateway and is
exercised by the opt-in E2E test, not here."""

from quantmind.datastore.store import BarStore
from quantmind.sync_cli import (
    DEFAULT_UNIVERSE,
    INDEX_UNIVERSE,
    WORLD_ETF_REGIONS,
    needed_fx_pairs,
)


def test_world_etfs_and_sh_are_in_default_universe():
    for symbol in ["EZU", "EWU", "EWY", "EWT", "INDA", "MCHI", "EWZ", "EEM", "EFA", "SH"]:
        assert symbol in DEFAULT_UNIVERSE
        assert symbol in WORLD_ETF_REGIONS


def test_default_universe_has_no_duplicates():
    assert len(DEFAULT_UNIVERSE) == len(set(DEFAULT_UNIVERSE))


def test_every_world_etf_region_is_a_non_empty_string():
    for symbol, region in WORLD_ETF_REGIONS.items():
        assert isinstance(region, str) and len(region) > 0


def test_index_universe_has_vix_and_spx_on_cboe():
    assert INDEX_UNIVERSE == {"VIX": "CBOE", "SPX": "CBOE"}


# --- FX-aware valuation: pair derivation from stored metadata currencies ---


def test_needed_fx_pairs_derives_deduped_pairs_from_metadata(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata("VWRP", {"currency": "GBP"})
    store.write_instrument_metadata("O", {"currency": "USD"})
    store.write_instrument_metadata("GOOG", {"currency": "USD"})  # dedups with O
    store.write_instrument_metadata("SPX", {"long_name": "S&P 500"})  # no currency — skipped
    assert needed_fx_pairs(store, "GBP") == ["GBPUSD"]
    assert needed_fx_pairs(store, "USD") == ["GBPUSD"]
    # A third base needs a pair per non-base currency, priority-ordered names.
    assert needed_fx_pairs(store, "EUR") == ["EURGBP", "EURUSD"]


def test_needed_fx_pairs_empty_for_a_single_currency_store(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata("SPY", {"currency": "USD"})
    assert needed_fx_pairs(store, "USD") == []


def test_needed_fx_pairs_empty_store_is_noop(tmp_path):
    assert needed_fx_pairs(BarStore(tmp_path), "GBP") == []
