"""Task A2: universe/config wiring in sync_cli — pure data, no I/O. The
async `main()` orchestration itself talks to a live IB Gateway and is
exercised by the opt-in E2E test, not here."""

from quantmind.sync_cli import DEFAULT_UNIVERSE, INDEX_UNIVERSE, WORLD_ETF_REGIONS


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
