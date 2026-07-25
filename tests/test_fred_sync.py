"""FRED -> store sync with unit normalization (rates percent -> decimal)."""
import pandas as pd

from quantmind.datastore.store import BarStore
from quantmind.sources.fred import sync_fred


def _pct(vals, start="2026-07-20"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)))


def fake_fetcher(series_id: str) -> pd.Series:
    data = {
        "DGS10": _pct([4.18, 4.20]),      # percent
        "DGS2": _pct([3.90, 3.92]),
        "DGS3MO": _pct([4.35, 4.36]),
        "WALCL": _pct([6.6e6, 6.61e6]),    # $mn
        "WTREGEN": _pct([8.0e5, 8.1e5]),   # $mn
        "RRPONTSYD": _pct([100.0, 98.0]),  # $bn
    }
    return data[series_id]


def test_sync_fred_normalizes_rates_to_decimal_and_stores_net_liquidity(tmp_path):
    store = BarStore(tmp_path)
    written = sync_fred(store, fetcher=fake_fetcher)
    assert set(written) == {"US10Y", "US2Y", "US3M", "NET_LIQUIDITY"}
    us10y = store.read_series("US10Y")
    assert abs(us10y.iloc[-1] - 0.0420) < 1e-9  # 4.20% -> decimal
    nl = store.read_series("NET_LIQUIDITY")
    # 6.61e6mn*1e-3 - 8.1e5mn*1e-3 - 98bn = 6610 - 810 - 98 = 5702 ($bn)
    assert abs(nl.iloc[-1] - 5702.0) < 1e-6
