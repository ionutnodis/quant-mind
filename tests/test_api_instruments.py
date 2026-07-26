"""API contract tests for the instruments domain (Task A2): metadata + derived
stats (52w range, ann vol, beta vs benchmark) and the OHLC candle window.
Serialization policy: UTC ISO timestamps, NaN -> null, unknown symbol -> 422,
never a 500 (pattern: tests/test_api_risk.py)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantmind.datastore.store import BarMeta, BarStore

# Load instruments.py directly from its file rather than via
# `quantmind.api.routers.instruments` (which forces Python to first run
# `quantmind/api/routers/__init__.py`, eagerly importing every sibling-owned
# router — several of which are mid-edit in this shared wave-3 tree at any
# given moment). instruments.py is this task's exclusive file and only
# imports from quantmind.risk (an unrelated, stable package), so this keeps
# these tests deterministic regardless of sibling task state.
_INSTRUMENTS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src"
    / "quantmind"
    / "api"
    / "routers"
    / "instruments.py"
)
_spec = importlib.util.spec_from_file_location("_instruments_under_test", _INSTRUMENTS_PATH)
_instruments_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _instruments_module  # pydantic resolves forward refs via sys.modules
_spec.loader.exec_module(_instruments_module)
instruments_router = _instruments_module.router


def _make_app(store: BarStore, benchmark: str = "SPY") -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.state.benchmark = benchmark
    app.include_router(instruments_router, prefix="/api")
    return app


def _bars(n=300, seed=1, price0=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = price0 * np.abs(np.cumprod(1 + rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0},
        index=idx,
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2, price0=50.0), meta=meta)
    store.write_symbol_map({"SPY": 1, "EEM": 2})
    store.write_instrument_metadata(
        "EEM",
        {
            "con_id": 2,
            "long_name": "iShares MSCI Emerging Markets ETF",
            "exchange": "ARCA",
            "currency": "USD",
            "sec_type": "STK",
            "industry": None,
            "region": "Emerging Markets",
            "provider": "ibkr",
        },
    )
    app = _make_app(store, benchmark="SPY")
    return TestClient(app, base_url="http://127.0.0.1")


def test_instrument_returns_metadata_and_derived_stats(client):
    r = client.get("/api/instruments/EEM")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "EEM"
    assert body["con_id"] == 2
    assert body["long_name"] == "iShares MSCI Emerging Markets ETF"
    assert body["exchange"] == "ARCA"
    assert body["region"] == "Emerging Markets"
    assert body["provider"] == "ibkr"
    assert body["last_close"] is not None
    assert body["high_52w"] is not None
    assert body["low_52w"] is not None
    # high/low are max/min over the trailing 52w window, which includes last_close.
    assert body["high_52w"] >= body["last_close"] >= body["low_52w"]
    assert body["ann_vol"] is None or body["ann_vol"] >= 0
    assert body["beta_benchmark"] == "SPY"
    assert body["as_of"] is not None


def test_instrument_missing_metadata_returns_nulls_not_crash(client):
    r = client.get("/api/instruments/SPY")
    assert r.status_code == 200
    body = r.json()
    assert body["long_name"] is None
    assert body["region"] is None
    # self-beta vs itself is exactly 1.0
    assert body["beta"] == pytest.approx(1.0)
    assert body["beta_benchmark"] == "SPY"


def test_instrument_unknown_symbol_is_422_not_500(client):
    r = client.get("/api/instruments/NOPE")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_instrument_mapped_but_barless_symbol_is_422(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_symbol_map({"SPY": 1, "GHOST": 99})
    app = _make_app(store, benchmark="SPY")
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/instruments/GHOST")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_candles_returns_ohlc_window_bounded_by_days(client):
    r = client.get("/api/instruments/SPY/candles", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert body["days"] == 30
    assert len(body["candles"]) == 30
    c = body["candles"][0]
    assert set(c.keys()) == {"date", "open", "high", "low", "close", "volume"}
    assert c["close"] is not None


def test_candles_default_days_and_unknown_symbol_422(client):
    r = client.get("/api/instruments/SPY/candles")
    assert r.status_code == 200
    assert r.json()["days"] == 180

    r2 = client.get("/api/instruments/NOPE/candles")
    assert r2.status_code == 422


def test_candles_bounds_reject_out_of_range(client):
    r = client.get("/api/instruments/SPY/candles", params={"days": 0})
    assert r.status_code == 422
    r2 = client.get("/api/instruments/SPY/candles", params={"days": 100_000})
    assert r2.status_code == 422


def test_instrument_beta_of_benchmark_series_correlates_when_identical(tmp_path):
    # A symbol whose returns exactly track the benchmark should show beta ~ 1.
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    bars = _bars(seed=5)
    store.write_bars(con_id=1, bar_size="1d", bars=bars, meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=bars.copy(), meta=meta)  # identical series
    store.write_symbol_map({"SPY": 1, "CLONE": 2})
    app = _make_app(store, benchmark="SPY")
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/instruments/CLONE")
    assert r.status_code == 200
    assert r.json()["beta"] == pytest.approx(1.0, abs=1e-6)
