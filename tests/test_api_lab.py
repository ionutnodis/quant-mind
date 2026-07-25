"""Lab bench API tests: POST /api/lab/apply (Task 3, parallel-pages plan).

Reuses /api/models/{name}/fit + /simulate (never duplicates their math) and
pipes the simulated factor paths through quantmind.exposure.bridge into a P&L
distribution. Wrong-unit exposure must be an explicit 422, never a silent
wrong number (see tests/test_exposure.py for the underlying contract)."""

import math

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
    store.write_symbol_map({"SPY": 1})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _fit(client, symbol="SPY", years=1):
    r = client.post("/api/models/ou/fit", json={"symbol": symbol, "years": years})
    assert r.status_code == 200
    return r.json()


def test_apply_rate_level_usd_per_bp_produces_finite_sane_numbers(client):
    fit = _fit(client)
    r = client.post(
        "/api/lab/apply",
        json={
            "model_name": "ou",
            "fit": fit,
            "horizon": 60,
            "n_paths": 2000,
            "seed": 7,
            "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("mean", "p5", "p50", "p95", "es"):
        assert body[key] is not None
        assert math.isfinite(body[key])
    assert body["horizon"] == 60
    assert body["n_paths"] == 2000
    hist = body["histogram"]
    assert len(hist["edges"]) == len(hist["counts"]) + 1
    assert sum(hist["counts"]) == 2000
    assert len(hist["counts"]) <= 60
    # ES is the average of the worst tail — never better than the p5 readout.
    assert body["es"] <= body["p5"] + 1e-6


def test_apply_mismatched_exposure_kind_is_422_with_refusing_message(client):
    fit = _fit(client)
    r = client.post(
        "/api/lab/apply",
        json={
            "model_name": "ou",
            "fit": fit,
            "horizon": 30,
            "n_paths": 500,
            "exposure": {"factor_kind": "vol_points", "units": "usd_per_volpt", "value": -184.0},
        },
    )
    assert r.status_code == 422
    assert "refusing" in r.json()["detail"]


def test_apply_bounds_reject_resource_exhaustion(client):
    fit = _fit(client)

    def apply(horizon, n_paths):
        return client.post(
            "/api/lab/apply",
            json={
                "model_name": "ou",
                "fit": fit,
                "horizon": horizon,
                "n_paths": n_paths,
                "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
            },
        )

    assert apply(60, 10_000_000).status_code == 422
    assert apply(100_000, 100).status_code == 422


def test_apply_unknown_model_is_404(client):
    fit = _fit(client)
    r = client.post(
        "/api/lab/apply",
        json={
            "model_name": "nope",
            "fit": fit,
            "horizon": 30,
            "n_paths": 500,
            "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
        },
    )
    assert r.status_code == 404


def test_apply_seeded_reproducible_across_calls(client):
    fit = _fit(client)

    def call():
        r = client.post(
            "/api/lab/apply",
            json={
                "model_name": "ou",
                "fit": fit,
                "horizon": 30,
                "n_paths": 500,
                "seed": 42,
                "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
            },
        )
        assert r.status_code == 200
        return r.json()

    a, b = call(), call()
    assert a["mean"] == b["mean"]
    assert a["es"] == b["es"]
    assert a["histogram"] == b["histogram"]
