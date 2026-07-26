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


def _bars_from_close(close: np.ndarray, idx: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _us10y_from(spy_close: pd.Series) -> pd.Series:
    """US10Y decimal levels engineered so the daily bp change EXACTLY tracks
    SPY's daily simple return (Δlevel·1e4 = ret + ~1e-4 bp of noise): a book
    of q SPY shares then has daily $P&L = gross·ret_t, so the regression of
    book $P&L on Δ(US10Y) bp must recover beta = gross (the golden value)."""
    ret = spy_close.pct_change().dropna()
    rng = np.random.default_rng(2)
    increments = ret / 10000.0 + rng.normal(0, 1e-8, len(ret))
    levels = pd.Series(0.04, index=spy_close.index, dtype=float)
    levels.iloc[1:] = 0.04 + increments.cumsum().to_numpy()
    return levels


def _pair_closes(n=300):
    """A cointegrated (AAA, BBB) pair — BBB = 20 + 0.5·AAA + stationary noise
    — and an independent walk CCC (not cointegrated with AAA)."""
    rng = np.random.default_rng(4)
    aaa = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
    bbb = 20.0 + 0.5 * aaa + rng.normal(0, 0.5, n)
    ccc = 50.0 + np.cumsum(np.random.default_rng(5).normal(0, 1.0, n))
    return aaa, bbb, ccc


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy, meta=meta)
    idx = spy.index
    aaa, bbb, ccc = _pair_closes(len(idx))
    store.write_bars(con_id=2, bar_size="1d", bars=_bars_from_close(aaa, idx), meta=meta)
    store.write_bars(con_id=3, bar_size="1d", bars=_bars_from_close(bbb, idx), meta=meta)
    store.write_bars(con_id=4, bar_size="1d", bars=_bars_from_close(ccc, idx), meta=meta)
    store.write_symbol_map({"SPY": 1, "AAA": 2, "BBB": 3, "CCC": 4})
    store.write_series("US10Y", _us10y_from(spy["close"]))
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
    assert len(hist["bin_edges"]) == len(hist["counts"]) + 1
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


def test_apply_explosive_fit_with_nonfinite_pnl_is_422_not_500(client):
    # The endpoint trusts client-supplied fit params (a round-tripped
    # FitResponse by design). An explosive OU (theta << 0) overflows the paths
    # to inf; np.histogram on non-finite data raises ValueError, which must
    # surface as a structured 422 — never a 500 (binding never-500 constraint).
    explosive_fit = {
        "model_name": "ou",
        "params": {"theta": -1e8, "mu": 0.04, "sigma": 0.02},
        "cis": {"theta": [-1e8, -1e8], "mu": [0.04, 0.04], "sigma": [0.02, 0.02]},
        "diagnostics": {"adf_pvalue": 1.0},
        "n_obs": 100,
    }
    r = client.post(
        "/api/lab/apply",
        json={
            "model_name": "ou",
            "fit": explosive_fit,
            "horizon": 60,
            "n_paths": 200,
            "seed": 1,
            "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
        },
    )
    assert r.status_code == 422
    assert "finite" in r.json()["detail"]


def test_apply_happy_path_reports_zero_nonfinite_paths(client):
    fit = _fit(client)
    r = client.post(
        "/api/lab/apply",
        json={
            "model_name": "ou",
            "fit": fit,
            "horizon": 30,
            "n_paths": 500,
            "seed": 3,
            "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
        },
    )
    assert r.status_code == 200
    assert r.json()["n_nonfinite"] == 0


# --- x0 defaults to the last observation (wave-3B spec item 3) ---


def test_apply_defaults_x0_to_last_observation(client):
    fit = _fit(client)
    x_last = fit["diagnostics"]["x_last"]

    def apply(x0=None):
        body = {
            "model_name": "ou",
            "fit": fit,
            "horizon": 30,
            "n_paths": 500,
            "seed": 5,
            "exposure": {"factor_kind": "rate_level", "units": "usd_per_bp", "value": -610.0},
        }
        if x0 is not None:
            body["x0"] = x0
        r = client.post("/api/lab/apply", json=body)
        assert r.status_code == 200
        return r.json()

    default = apply()
    explicit_last = apply(x0=x_last)
    assert default["mean"] == explicit_last["mean"]  # default start = last obs
    shifted = apply(x0=x_last + 5.0)
    assert shifted["mean"] != default["mean"]  # still overridable


# --- book-derived exposure regression (wave-3B spec item 1) ---


def _book_regression(client, **overrides):
    body = {"book": [{"symbol": "SPY", "qty": 10}], "factor_series": "US10Y", "years": 5}
    body.update(overrides)
    return client.post("/api/lab/book-regression", json=body)


def test_book_regression_recovers_engineered_rate_sensitivity(client):
    r = _book_regression(client)
    assert r.status_code == 200
    body = r.json()
    # Golden value: US10Y is engineered so Δbp == SPY return, hence
    # beta = book gross = qty * last close (see _us10y_from).
    gross = 10.0 * float(_bars(seed=1)["close"].iloc[-1])
    assert body["beta_usd_per_bp"] == pytest.approx(gross, rel=1e-2)
    assert body["book_gross"] == pytest.approx(gross, rel=1e-6)
    assert body["r_squared"] > 0.99
    # Uncertainty is displayed: SE + CI ship with the estimate.
    assert body["beta_se"] is not None and body["beta_se"] >= 0
    lo, hi = body["beta_ci"]
    assert lo <= body["beta_usd_per_bp"] <= hi
    # Horizon labeled, units explicit (feeds Apply's usd_per_bp exposure).
    assert body["horizon"] == "daily"
    assert body["exposure_units"] == "usd_per_bp"
    assert body["factor_series"] == "US10Y"
    assert body["n_obs"] >= 30
    assert body["hac_lags"] >= 0
    assert body["as_of"].endswith("Z")


def test_book_regression_accepts_book_ref(client):
    pin = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]})
    assert pin.status_code == 200
    ref = pin.json()["snapshot_id"]
    via_ref = _book_regression(client, book=None, book_ref=ref)
    assert via_ref.status_code == 200
    inline = _book_regression(client)
    assert via_ref.json()["beta_usd_per_bp"] == pytest.approx(
        inline.json()["beta_usd_per_bp"], rel=1e-9
    )


def test_book_regression_requires_exactly_one_of_book_or_book_ref(client):
    both = _book_regression(client, book_ref="abcdefabcdef")
    assert both.status_code == 422
    neither = client.post(
        "/api/lab/book-regression", json={"factor_series": "US10Y", "years": 5}
    )
    assert neither.status_code == 422


def test_book_regression_unknown_symbol_is_422(client):
    r = _book_regression(client, book=[{"symbol": "NOPE", "qty": 1}])
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_book_regression_unknown_factor_series_is_422(client):
    r = _book_regression(client, factor_series="NOSERIES")
    assert r.status_code == 422
    assert "NOSERIES" in r.json()["detail"]


def test_book_regression_unknown_book_ref_is_422(client):
    r = _book_regression(client, book=None, book_ref="0123456789ab")
    assert r.status_code == 422


def test_book_regression_nan_ci_serialized_as_null_not_500(client, monkeypatch):
    # Fix round 1 (bundled minor): a degenerate HAC fit can put NaN inside
    # beta_ci/beta_se — those must serialize as null (repo NaN→null policy),
    # never crash JSON encoding. Unit-level exercise of the response-building
    # path: wrap the real regression and poison its uncertainty fields.
    import dataclasses

    import quantmind.api.routers.lab as lab_router

    real = lab_router.factor_regression

    def poisoned(y, factors, **kwargs):
        result = real(y, factors, **kwargs)
        name = next(iter(factors))
        return dataclasses.replace(
            result,
            beta_ci={name: (float("nan"), float("nan"))},
            beta_se={name: float("nan")},
        )

    monkeypatch.setattr(lab_router, "factor_regression", poisoned)
    r = _book_regression(client)
    assert r.status_code == 200
    body = r.json()
    assert body["beta_ci"] is None
    assert body["beta_se"] is None
    assert body["beta_usd_per_bp"] is not None  # the estimate itself survives


# --- Batch-2 final review, item 2: option-leg parity in book-regression ---


def test_book_regression_full_opt_book_scales_gross_by_multiplier_with_note(client):
    # 10 SPY calls (multiplier 100) = the underlier notional of 1000 shares:
    # gross and the estimated $/bp beta must both scale 100x vs the share
    # book, and the delta-one approximation must be declared in `notes`.
    r_stk = _book_regression(client)
    r_opt = _book_regression(
        client,
        book=[{"symbol": "SPY", "qty": 10, "strike": 400.0, "expiry": "20260918", "right": "C"}],
    )
    assert r_stk.status_code == r_opt.status_code == 200
    body_stk, body_opt = r_stk.json(), r_opt.json()
    assert body_opt["book_gross"] == pytest.approx(100.0 * body_stk["book_gross"], rel=1e-9)
    assert body_opt["beta_usd_per_bp"] == pytest.approx(
        100.0 * body_stk["beta_usd_per_bp"], rel=1e-6
    )
    assert any("delta-one" in n for n in body_opt["notes"])
    assert body_stk["notes"] == []


def test_book_regression_option_book_ref_matches_inline(client):
    opt_book = [{"symbol": "SPY", "qty": 10, "strike": 400.0, "expiry": "20260918", "right": "C"}]
    pin = client.post("/api/book/pin", json={"positions": opt_book})
    assert pin.status_code == 200
    via_ref = _book_regression(client, book=None, book_ref=pin.json()["snapshot_id"])
    inline = _book_regression(client, book=opt_book)
    assert via_ref.status_code == inline.status_code == 200
    assert via_ref.json()["beta_usd_per_bp"] == pytest.approx(
        inline.json()["beta_usd_per_bp"], rel=1e-9
    )
    assert via_ref.json()["book_gross"] == pytest.approx(inline.json()["book_gross"], rel=1e-9)


def test_book_regression_inline_partial_option_descriptor_is_422(client):
    r = _book_regression(client, book=[{"symbol": "SPY", "qty": 10, "right": "C"}])
    assert r.status_code == 422
    assert "together" in r.json()["detail"]


# --- EG→OU pair pipeline (wave-3B spec item 5) ---


def test_pair_pipeline_cointegrated_pair_full_readout(client):
    r = client.post("/api/lab/pair", json={"y_symbol": "BBB", "x_symbol": "AAA", "years": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["coint_pvalue"] < 0.05
    assert body["is_cointegrated"] is True
    assert body["hedge_ratio"] == pytest.approx(0.5, abs=0.05)
    assert body["hedge_ratio_se"] is not None and body["hedge_ratio_se"] > 0
    # OU on the spread: bands + current displacement + half-life, all present.
    assert body["mu"] is not None
    assert body["stationary_sigma"] is not None and body["stationary_sigma"] > 0
    assert body["current_z"] is not None
    assert body["half_life_days"] is not None and body["half_life_days"] > 0
    lo, hi = body["half_life_ci"]
    assert lo <= body["half_life_days"] <= hi
    assert body["mean_reversion_established"] is True
    # Chart payload: aligned dates/values, ISO-Z stamped.
    assert len(body["dates"]) == len(body["spread"]) > 0
    assert body["dates"][-1].endswith("Z")
    assert body["as_of"].endswith("Z")
    assert body["horizon"] == "daily"
    # Full mathematical transparency: the raw OU fit rides along.
    assert body["fit"]["model_name"] == "ou"
    assert "theta" in body["fit"]["params"]


def test_pair_pipeline_honest_on_independent_walks(client):
    r = client.post("/api/lab/pair", json={"y_symbol": "CCC", "x_symbol": "AAA", "years": 5})
    assert r.status_code == 200  # structured honesty, never a 500
    body = r.json()
    assert body["is_cointegrated"] is False
    assert body["coint_pvalue"] > 0.05


def test_pair_pipeline_unknown_symbol_is_422(client):
    r = client.post("/api/lab/pair", json={"y_symbol": "NOPE", "x_symbol": "AAA", "years": 5})
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_pair_pipeline_same_symbol_is_422(client):
    r = client.post("/api/lab/pair", json={"y_symbol": "AAA", "x_symbol": "AAA", "years": 5})
    assert r.status_code == 422


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
