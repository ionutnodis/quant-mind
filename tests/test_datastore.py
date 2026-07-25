import numpy as np
import pandas as pd
import pytest

from quantmind.datastore.store import BarMeta, BarStore


def _bars(start, n, price0=100.0):
    idx = pd.bdate_range(start, periods=n)
    close = price0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
        index=idx,
    )


META = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-25", rth_only=True)


def test_round_trip_preserves_bars_and_metadata(tmp_path):
    store = BarStore(tmp_path)
    df = _bars("2026-01-05", 10)
    store.write_bars(con_id=265598, bar_size="1d", bars=df, meta=META)
    got, meta = store.read_bars(con_id=265598, bar_size="1d")
    pd.testing.assert_frame_equal(got, df, check_freq=False)
    assert meta.bar_type == "ADJUSTED_LAST"
    assert meta.adjusted_asof == "2026-07-25"
    assert meta.rth_only is True


def test_partitioned_one_file_per_conid_and_bar_size(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(con_id=1, bar_size="1d", bars=_bars("2026-01-05", 3), meta=META)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars("2026-01-05", 3), meta=META)
    assert (tmp_path / "bars" / "1d" / "1.parquet").exists()
    assert (tmp_path / "bars" / "1d" / "2.parquet").exists()


def test_watermark_is_last_bar_date(tmp_path):
    store = BarStore(tmp_path)
    df = _bars("2026-01-05", 5)
    store.write_bars(con_id=1, bar_size="1d", bars=df, meta=META)
    assert store.watermark(con_id=1, bar_size="1d") == df.index[-1]


def test_watermark_missing_instrument_is_none(tmp_path):
    store = BarStore(tmp_path)
    assert store.watermark(con_id=999, bar_size="1d") is None


def test_rewrite_replaces_history_for_readjustment(tmp_path):
    # After a split, adjusted history REWRITES past bars — the store must replace, not append.
    store = BarStore(tmp_path)
    store.write_bars(con_id=1, bar_size="1d", bars=_bars("2026-01-05", 5, price0=400.0), meta=META)
    post_split = _bars("2026-01-05", 5, price0=100.0)  # 4:1 split re-adjustment
    store.write_bars(con_id=1, bar_size="1d", bars=post_split, meta=META)
    got, _ = store.read_bars(con_id=1, bar_size="1d")
    pd.testing.assert_frame_equal(got, post_split, check_freq=False)


def test_read_missing_instrument_raises_clear_error(tmp_path):
    store = BarStore(tmp_path)
    with pytest.raises(FileNotFoundError, match="con_id 42"):
        store.read_bars(con_id=42, bar_size="1d")


# --- instrument metadata (Task A2): contract-details cache, symbol -> dict,
# merge-write so a later refresh (e.g. new region tag) doesn't clobber fields
# a previous sync already wrote.

def test_instrument_metadata_missing_symbol_is_none(tmp_path):
    store = BarStore(tmp_path)
    assert store.read_instrument_metadata("SPY") is None
    assert store.read_all_instrument_metadata() == {}


def test_instrument_metadata_round_trip(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata(
        "SPY", {"con_id": 756733, "long_name": "SPDR S&P 500", "exchange": "ARCA", "provider": "ibkr"}
    )
    got = store.read_instrument_metadata("SPY")
    assert got == {"con_id": 756733, "long_name": "SPDR S&P 500", "exchange": "ARCA", "provider": "ibkr"}


def test_instrument_metadata_write_merges_not_overwrites(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata("EEM", {"con_id": 1, "long_name": "iShares EM ETF"})
    store.write_instrument_metadata("EEM", {"region": "Emerging Markets"})
    got = store.read_instrument_metadata("EEM")
    assert got == {"con_id": 1, "long_name": "iShares EM ETF", "region": "Emerging Markets"}


def test_instrument_metadata_does_not_disturb_other_symbols(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata("SPY", {"con_id": 1})
    store.write_instrument_metadata("QQQ", {"con_id": 2})
    all_meta = store.read_all_instrument_metadata()
    assert set(all_meta) == {"SPY", "QQQ"}
    assert all_meta["SPY"]["con_id"] == 1
    assert all_meta["QQQ"]["con_id"] == 2
