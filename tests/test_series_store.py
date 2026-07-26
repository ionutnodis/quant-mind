"""Generic named-series cache (FRED etc.) — wave-2 prep."""
import pandas as pd
import pytest

from quantmind.datastore.store import BarStore


def _s(n=5, start="2026-07-20"):
    return pd.Series([1.0 * i for i in range(n)], index=pd.bdate_range(start, periods=n))


def test_series_round_trip(tmp_path):
    store = BarStore(tmp_path)
    store.write_series("US10Y", _s())
    got = store.read_series("US10Y")
    pd.testing.assert_series_equal(got, _s(), check_freq=False, check_names=False)


def test_series_replace_semantics(tmp_path):
    store = BarStore(tmp_path)
    store.write_series("US10Y", _s(3))
    store.write_series("US10Y", _s(5))
    assert len(store.read_series("US10Y")) == 5


def test_missing_series_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="US10Y"):
        BarStore(tmp_path).read_series("US10Y")


def test_list_series(tmp_path):
    store = BarStore(tmp_path)
    assert store.list_series() == []
    store.write_series("US10Y", _s())
    store.write_series("NET_LIQUIDITY", _s())
    assert store.list_series() == ["NET_LIQUIDITY", "US10Y"]
