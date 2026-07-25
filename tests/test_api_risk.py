"""API contract tests for the risk domain routes: rolling beta/alpha + ES/vol,
and block-bootstrap Monte Carlo. Serialization policy: UTC ISO timestamps,
NaN -> null, unknown symbol/insufficient data -> structured 422, never a 500."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore


def _bars(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2), meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_risk_beta_of_benchmark_vs_itself_is_one(client):
    r = client.get("/api/risk/SPY", params={"window": 30, "years": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert body["benchmark"] == "SPY"
    series = body["beta_series"]
    assert len(series) > 0
    vals = [p["beta"] for p in series if p["beta"] is not None]
    assert len(vals) > 0
    assert all(v == pytest.approx(1.0, abs=1e-6) for v in vals)
    assert all(p["date"] for p in series)


def test_risk_returns_es_vol_and_alpha_note(client):
    r = client.get("/api/risk/QQQ", params={"window": 30, "years": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["es_975"] is None or body["es_975"] >= 0
    assert body["ann_vol"] is None or body["ann_vol"] >= 0
    assert body["alpha_note"] == "vs SPY, rf=0 until FRED wiring"
    assert body["window"] == 30
    assert body["years"] == 1


def test_risk_downsamples_beta_series_to_500_points(client):
    r = client.get("/api/risk/QQQ", params={"window": 5, "years": 5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["beta_series"]) <= 500


def test_risk_unknown_symbol_is_422_not_500(client):
    r = client.get("/api/risk/NOPE")
    assert r.status_code == 422
    assert "detail" in r.json()


@pytest.fixture
def client_with_mapped_but_barless_symbol(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    # GHOST is in the symbol map but has no cached bars at any bar size.
    store.write_symbol_map({"SPY": 1, "GHOST": 99})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_risk_mapped_symbol_without_bars_is_422_not_500(client_with_mapped_but_barless_symbol):
    r = client_with_mapped_but_barless_symbol.get("/api/risk/GHOST")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_montecarlo_mapped_symbol_without_bars_is_422_not_500(client_with_mapped_but_barless_symbol):
    r = client_with_mapped_but_barless_symbol.post(
        "/api/risk/montecarlo", json={"symbol": "GHOST", "horizon": 21, "n_paths": 1000}
    )
    assert r.status_code == 422
    assert "detail" in r.json()


def test_risk_window_bounds_reject_out_of_range(client):
    r = client.get("/api/risk/SPY", params={"window": 0})
    assert r.status_code == 422
    r2 = client.get("/api/risk/SPY", params={"window": 100_000})
    assert r2.status_code == 422


def test_risk_years_bounds_reject_out_of_range(client):
    r = client.get("/api/risk/SPY", params={"years": 0})
    assert r.status_code == 422
    r2 = client.get("/api/risk/SPY", params={"years": 100})
    assert r2.status_code == 422


def test_montecarlo_returns_histogram_and_percentiles(client):
    r = client.post(
        "/api/risk/montecarlo", json={"symbol": "SPY", "horizon": 21, "n_paths": 2000, "seed": 7}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert body["horizon"] == 21
    assert body["n_paths"] == 2000
    hist = body["histogram"]
    assert len(hist["bin_edges"]) == len(hist["counts"]) + 1
    assert len(hist["counts"]) <= 60
    assert sum(hist["counts"]) == 2000
    assert body["p5"] <= body["p50"] <= body["p95"]
    assert body["es_975"] is not None


def test_montecarlo_seeded_run_is_reproducible(client):
    payload = {"symbol": "SPY", "horizon": 21, "n_paths": 1000, "seed": 42}
    r1 = client.post("/api/risk/montecarlo", json=payload)
    r2 = client.post("/api/risk/montecarlo", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_montecarlo_unknown_symbol_is_422(client):
    r = client.post("/api/risk/montecarlo", json={"symbol": "NOPE", "horizon": 21, "n_paths": 1000})
    assert r.status_code == 422


def test_montecarlo_bounds_reject_resource_exhaustion(client):
    r = client.post(
        "/api/risk/montecarlo",
        json={"symbol": "SPY", "horizon": 21, "n_paths": 10_000_000, "seed": 1},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/risk/montecarlo",
        json={"symbol": "SPY", "horizon": 100_000, "n_paths": 100, "seed": 1},
    )
    assert r2.status_code == 422


def _bars_with_zero_close(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    # A single degenerate zero close (bad tick / corporate-action artifact in
    # cached bars) makes pct_change() emit inf, which compounds through the
    # block bootstrap into non-finite terminal draws.
    close[150] = 0.0
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def client_with_zero_close(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars_with_zero_close(seed=1), meta=meta)
    store.write_symbol_map({"SPY": 1, "ZERO": 1})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_montecarlo_zero_close_never_500s_and_reports_n_nonfinite(client_with_zero_close):
    # Reproduces the finite-guard gap: np.histogram used to raise ValueError
    # (autodetected range of [nan, nan] is not finite) -> unhandled -> 500.
    # Match lab's semantics (src/quantmind/api/routers/lab.py): drop
    # non-finite draws, report how many via n_nonfinite, 422 only if nothing
    # finite remains.
    r = client_with_zero_close.post(
        "/api/risk/montecarlo",
        json={"symbol": "ZERO", "horizon": 21, "n_paths": 2000, "seed": 7},
    )
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        body = r.json()
        assert "n_nonfinite" in body
        assert body["n_nonfinite"] > 0
        hist = body["histogram"]
        assert sum(hist["counts"]) == 2000 - body["n_nonfinite"]
    else:
        assert "detail" in r.json()
