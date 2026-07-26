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


def _named_series(n=300, seed=1, start=0.04, scale=0.0005):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    levels = start + np.cumsum(rng.normal(0, scale, n))
    return pd.Series(levels, index=idx)


@pytest.fixture
def client_with_named_series(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2), meta=meta)
    store.write_symbol_map({"SPY": 1, "MTUM": 2})
    store.write_series("US10Y", _named_series(seed=3))
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_regression_single_factor_capm_shape(client_with_named_series):
    r = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "years": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MTUM"
    assert body["factors"] == ["SPY"]
    assert body["n_obs"] > 0
    assert body["hac_lags"] >= 0
    assert len(body["scatter"]) > 0
    assert all("date" in p and "asset" in p and "factor" in p for p in body["scatter"])
    assert body["fit_line"]["factor"] == "SPY"
    assert body["betas"][0]["factor"] == "SPY"
    assert body["betas"][0]["ci_low"] <= body["betas"][0]["beta"] <= body["betas"][0]["ci_high"]
    assert body["r_squared"] is not None
    assert len(body["r_squared_progression"]) == 1
    names = {row["name"] for row in body["variance_decomposition"]}
    assert names == {"SPY", "idiosyncratic"}
    shares = [row["share"] for row in body["variance_decomposition"] if row["share"] is not None]
    assert sum(shares) == pytest.approx(1.0, abs=1e-3)
    attribution_names = {row["name"] for row in body["attribution"]}
    assert attribution_names == {"alpha", "SPY", "idiosyncratic"}
    assert len(body["residuals"]) > 0
    assert body["as_of"]


def test_regression_multi_factor_includes_named_rate_series(client_with_named_series):
    r = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY,US10Y", "years": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["factors"] == ["SPY", "US10Y"]
    assert {b["factor"] for b in body["betas"]} == {"SPY", "US10Y"}
    assert [step["factor_added"] for step in body["r_squared_progression"]] == ["SPY", "US10Y"]
    # Adding a factor can only raise (never lower) in-sample R^2.
    assert body["r_squared_progression"][1]["r_squared"] >= body["r_squared_progression"][0]["r_squared"] - 1e-9
    # The scatter/fit-line stay pinned to the first factor even with two factors requested.
    assert body["fit_line"]["factor"] == "SPY"


def test_regression_unknown_factor_is_422_not_500(client_with_named_series):
    r = client_with_named_series.get("/api/risk/MTUM/regression", params={"factors": "NOPE"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_regression_duplicate_factor_names_is_422(client_with_named_series):
    r = client_with_named_series.get("/api/risk/MTUM/regression", params={"factors": "SPY,SPY"})
    assert r.status_code == 422


def test_regression_unknown_symbol_is_422_not_500(client_with_named_series):
    r = client_with_named_series.get("/api/risk/NOPE/regression", params={"factors": "SPY"})
    assert r.status_code == 422


def test_regression_window_param_trims_observations(client_with_named_series):
    full = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "years": 1}
    ).json()
    windowed = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "years": 1, "window": 40}
    ).json()
    assert windowed["n_obs"] == 40
    assert windowed["n_obs"] < full["n_obs"]
    assert windowed["window"] == 40
    assert full["window"] is None


def test_regression_window_bounds_reject_out_of_range(client_with_named_series):
    r = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "window": 1}
    )
    assert r.status_code == 422
    r2 = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "window": 100_000}
    )
    assert r2.status_code == 422


def test_regression_insufficient_overlap_is_422_not_500(client_with_named_series):
    r = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "years": 1, "window": 20}
    )
    assert r.status_code == 422
    assert "detail" in r.json()


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


@pytest.fixture
def client_with_zero_close_regression(tmp_path):
    """SPY normal, ZERO carries a single zero close — one +/-inf daily
    return that dropna() alone never removes (batch-1 final review F2)."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars_with_zero_close(seed=2), meta=meta)
    store.write_symbol_map({"SPY": 1, "ZERO": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_regression_zero_close_inf_return_never_500s(client_with_zero_close_regression):
    # The zero close's inf return reaches factor_regression (the router's
    # dropna() doesn't drop inf); statsmodels raised MissingDataError —
    # which `except InsufficientDataError` misses — -> 500. The pure core
    # must treat non-finite rows as missing data instead.
    r = client_with_zero_close_regression.get(
        "/api/risk/ZERO/regression", params={"factors": "SPY", "years": 2}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["r_squared"] is not None
    assert body["n_obs"] > 0


def test_regression_factor_list_capped_at_10(client_with_named_series):
    # Resource guard (F10): the factors list length is bounded like every
    # other tunable — 11 factors is a structured 422, not an open-ended fit.
    factors = ",".join(f"F{i:02d}" for i in range(11))
    r = client_with_named_series.get("/api/risk/MTUM/regression", params={"factors": factors})
    assert r.status_code == 422
    assert "at most 10" in str(r.json()["detail"])


def test_regression_window_floor_matches_core_minimum(client_with_named_series):
    # The Query floor now IS the core's 30-obs minimum (single shared
    # constant — adjudication d): windows 20-29 used to pass validation and
    # then ALWAYS 422 as a data error. Now they're rejected as input.
    r = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "window": 29}
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # FastAPI input validation, not a data error

    r2 = client_with_named_series.get(
        "/api/risk/MTUM/regression", params={"factors": "SPY", "years": 1, "window": 30}
    )
    assert r2.status_code == 200
    assert r2.json()["n_obs"] == 30
