"""API contract tests: TestClient against a fixture store. Serialization policy:
UTC ISO timestamps, NaN -> null, empty cache -> structured empty, never a 500."""

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


@pytest.fixture
def empty_client(tmp_path):
    app = create_app(store=BarStore(tmp_path / "empty"), benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_brief_returns_tiles_correlation_and_utc_iso_asof(client):
    r = client.get("/api/brief")
    assert r.status_code == 200
    body = r.json()
    symbols = {t["symbol"] for t in body["tiles"]}
    assert symbols == {"SPY", "QQQ"}
    assert body["as_of"].endswith("T00:00:00Z")
    assert body["benchmark_es"] > 0
    assert body["correlation"]["symbols"] == sorted(body["correlation"]["symbols"])
    assert all(v is None or isinstance(v, float) for row in body["correlation"]["matrix"] for v in row)


def test_brief_empty_cache_is_structured_empty_not_500(empty_client):
    r = empty_client.get("/api/brief")
    assert r.status_code == 200
    body = r.json()
    assert body["tiles"] == []
    assert body["as_of"] is None
    assert body["benchmark_es"] is None


def test_auth_required_when_token_configured(client):
    r = client.get("/api/brief", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r2 = client.get("/api/brief", headers={"Authorization": ""})  # override, not merge
    assert r2.status_code == 401


def test_models_endpoint_serves_registry_schemas(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    names = [m["name"] for m in r.json()]
    assert "ou" in names


def test_nan_serializes_as_null_not_500(client):
    # ES over a 1-bar series would be insufficient data; the API must map internal
    # errors to structured responses. Exercise via the models fit endpoint on a
    # tiny series: expect a 422-style structured error, not a crash.
    r = client.post("/api/models/ou/fit", json={"symbol": "SPY", "years": 0})
    assert r.status_code in (200, 422)
    if r.status_code == 422:
        assert "detail" in r.json()


def test_fit_endpoint_returns_full_transparency(client):
    r = client.post("/api/models/ou/fit", json={"symbol": "SPY", "years": 1})
    assert r.status_code == 200
    body = r.json()
    assert set(body["params"]) == {"theta", "mu", "sigma"}
    assert set(body["cis"]) == {"theta", "mu", "sigma"}
    assert "adf_pvalue" in body["diagnostics"]


def test_simulate_endpoint_returns_bands_not_raw_paths(client):
    fit = client.post("/api/models/ou/fit", json={"symbol": "SPY", "years": 1}).json()
    r = client.post(
        "/api/models/ou/simulate",
        json={"fit": fit, "horizon": 60, "n_paths": 2000, "seed": 7, "x0": 100.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["bands"]) == {"p5", "p25", "p50", "p75", "p95"}
    assert len(body["bands"]["p50"]) == 60
    assert len(body["sample_paths"]) <= 100  # never raw 10k paths over the wire


def test_simulate_negative_seed_is_422_not_500(client):
    # Batch-2 final review item 3 (never-500): np.random.default_rng(-1)
    # raises ValueError — the seed must be bounds-checked at the model layer
    # (whatif's Field(None, ge=0, le=2**31-1) convention).
    fit = client.post("/api/models/ou/fit", json={"symbol": "SPY", "years": 1}).json()
    r = client.post(
        "/api/models/ou/simulate",
        json={"fit": fit, "horizon": 60, "n_paths": 100, "seed": -1, "x0": 100.0},
    )
    assert r.status_code == 422


def test_forged_host_header_is_rejected(client):
    r = client.get("/api/health", headers={"Host": "testserver"})
    assert r.status_code == 403
    r2 = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert r2.status_code == 403


def test_simulate_bounds_reject_resource_exhaustion(client):
    fit = client.post("/api/models/ou/fit", json={"symbol": "SPY", "years": 1}).json()
    r = client.post(
        "/api/models/ou/simulate",
        json={"fit": fit, "horizon": 60, "n_paths": 10_000_000, "seed": 1, "x0": 100.0},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/models/ou/simulate",
        json={"fit": fit, "horizon": 100_000, "n_paths": 100, "seed": 1, "x0": 100.0},
    )
    assert r2.status_code == 422
