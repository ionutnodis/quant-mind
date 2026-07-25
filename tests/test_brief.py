import numpy as np
import pandas as pd
import pytest

from quantmind.brief import build_brief
from quantmind.datastore.store import BarMeta, BarStore


def _bars(n=300, last_close=110.0, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    close[-1] = last_close
    close[-2] = 100.0  # pin the 1d change to +10%
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0},
        index=idx,
    )


@pytest.fixture
def store(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2), meta=meta)
    store.write_bars(con_id=3, bar_size="1d", bars=_bars(seed=3), meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2, "GLD": 3})
    return store


def test_brief_tiles_have_last_close_and_1d_change(store):
    brief = build_brief(store, benchmark="SPY")
    tile = next(t for t in brief.tiles if t.symbol == "QQQ")
    assert tile.last_close == pytest.approx(110.0)
    assert tile.change_1d == pytest.approx(0.10)


def test_brief_correlation_matrix_covers_universe(store):
    brief = build_brief(store, benchmark="SPY")
    assert set(brief.correlation.columns) == {"SPY", "QQQ", "GLD"}
    assert np.allclose(np.diag(brief.correlation), 1.0)


def test_brief_benchmark_es_is_sane(store):
    brief = build_brief(store, benchmark="SPY")
    assert 0 < brief.benchmark_es < 0.2


def test_brief_as_of_is_latest_bar_date(store):
    brief = build_brief(store, benchmark="SPY")
    assert brief.as_of == pd.Timestamp("2026-07-24")


def test_brief_empty_store_yields_empty_brief(tmp_path):
    brief = build_brief(BarStore(tmp_path), benchmark="SPY")
    assert brief.tiles == []
    assert brief.benchmark_es is None
    assert brief.as_of is None
