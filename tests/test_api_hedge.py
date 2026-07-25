"""API contract tests for the Hedge Lab route: POST /api/hedge ranks sized
hedge candidates against a beta_target objective. Cointegration is a
DIAGNOSTIC column only (Engineering Constraint 12) — ranking is strictly by
protection (ES reduction), never by cointegration. Serialization policy:
UTC ISO timestamps, NaN -> null, unknown symbol/empty candidates/bounds ->
structured 422, never a 500."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.risk.returns import rolling_beta


def _bars(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _flat_bars(n=300, price=100.0):
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _beta_correlated_bars(spy_close: pd.Series, beta: float, noise_scale: float, seed: int, start_price=100.0):
    """Build a synthetic instrument whose returns are `beta * spy_returns + idiosyncratic
    noise`, so its true beta vs SPY is deterministically well above the 0.1 unusable
    threshold (unlike two independently-seeded random walks, whose sample beta over a
    finite window can land anywhere near zero by chance)."""
    rng = np.random.default_rng(seed)
    spy_returns = spy_close.pct_change().dropna().to_numpy()
    noise = rng.normal(0, noise_scale, len(spy_returns))
    rets = beta * spy_returns + noise
    close = np.empty(len(spy_close))
    close[0] = start_price
    close[1:] = start_price * np.cumprod(1 + rets)
    idx = spy_close.index
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)  # SPY
    store.write_bars(
        con_id=2, bar_size="1d", bars=_beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2), meta=meta
    )  # QQQ
    store.write_bars(
        con_id=3, bar_size="1d", bars=_beta_correlated_bars(spy_bars["close"], beta=0.5, noise_scale=0.003, seed=3), meta=meta
    )  # IWM
    store.write_bars(con_id=4, bar_size="1d", bars=_flat_bars(), meta=meta)  # FLAT (~zero beta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2, "IWM": 3, "FLAT": 4})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_hedge_single_candidate_sizing_matches_formula(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["benchmark"] == "SPY"
    assert body["n_candidates_evaluated"] == 1
    assert len(body["candidates"]) == 1
    cand = body["candidates"][0]
    assert cand["symbol"] == "QQQ"
    assert cand["unusable"] is False

    # Independently recompute candidate beta the same way the router does:
    # years=1 -> tail 252 rows of cached close, simple returns, inner-joined
    # with the benchmark's returns, rolling_beta(window=60), last value.
    spy_bars = _bars(seed=1)
    qqq_bars = _beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2)
    spy_close = spy_bars["close"].iloc[-252:]
    qqq_close = qqq_bars["close"].iloc[-252:]
    spy_ret = spy_close.pct_change().dropna()
    qqq_ret = qqq_close.pct_change().dropna()
    aligned = pd.concat({"asset": qqq_ret, "bench": spy_ret}, axis=1).dropna()
    beta_series = rolling_beta(aligned["asset"], aligned["bench"], window=60, rf=0.0).dropna()
    beta_cand_expected = float(beta_series.iloc[-1])
    assert cand["beta"] == pytest.approx(beta_cand_expected, rel=1e-9)

    # book == benchmark == SPY exactly -> book_beta is beta of SPY vs itself (~1.0).
    assert body["book_beta"] == pytest.approx(1.0, abs=1e-6)
    book_value = body["book_value"]
    assert book_value == pytest.approx(10 * float(spy_close.iloc[-1]))

    price_cand_last = float(qqq_close.iloc[-1])
    expected_raw_size = (body["book_beta"] - 0.0) * book_value / (beta_cand_expected * price_cand_last)
    expected_hedge_qty = -expected_raw_size
    assert cand["hedge_qty"] == pytest.approx(expected_hedge_qty, rel=1e-6)
    assert cand["hedge_notional"] == pytest.approx(expected_hedge_qty * price_cand_last, rel=1e-6)
    assert cand["es_before"] == body["es_before"]
    assert cand["protection"] is not None


def test_hedge_ranks_candidates_by_protection_desc(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": -1.0},
            "candidates": ["QQQ", "IWM", "FLAT"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["n_candidates_evaluated"] == 3
    protections = [c["protection"] for c in body["candidates"]]
    # Non-None protections must appear first, sorted descending; None (unusable) last.
    seen_none = False
    prev = None
    for p in protections:
        if p is None:
            seen_none = True
            continue
        assert not seen_none, "a non-None protection appeared after a None one"
        if prev is not None:
            assert p <= prev
        prev = p
    # FLAT is unusable (~zero beta) so it must be flagged and ranked last.
    flat = next(c for c in body["candidates"] if c["symbol"] == "FLAT")
    assert flat["unusable"] is True
    assert body["candidates"][-1]["symbol"] == "FLAT"


def test_hedge_unusable_candidate_flagged_not_errored(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FLAT"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    cand = body["candidates"][0]
    assert cand["symbol"] == "FLAT"
    assert cand["unusable"] is True
    assert abs(cand["beta"]) < 0.1
    assert cand["hedge_qty"] is None
    assert cand["hedge_notional"] is None
    assert cand["protection"] is None


def test_hedge_default_candidate_universe_excludes_book_symbols(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    symbols = {c["symbol"] for c in body["candidates"]}
    assert "SPY" not in symbols
    assert symbols == {"QQQ", "IWM", "FLAT"}


def test_hedge_unknown_book_symbol_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "NOPE", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_hedge_unknown_candidate_symbol_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["NOPE"],
        },
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_hedge_empty_candidate_universe_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [
                {"symbol": "SPY", "qty": 10},
                {"symbol": "QQQ", "qty": 1},
                {"symbol": "IWM", "qty": 1},
                {"symbol": "FLAT", "qty": 1},
            ],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422
    assert "detail" in r.json()


def test_hedge_book_bounds_reject_empty_and_oversized(client):
    r = client.post(
        "/api/hedge",
        json={"book": [], "objective": {"kind": "beta_target", "value": 0.0}},
    )
    assert r.status_code == 422

    r2 = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": i + 1} for i in range(51)],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r2.status_code == 422


def test_hedge_objective_value_bounds_reject_out_of_range(client):
    r = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 3.0}},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": -3.0}},
    )
    assert r2.status_code == 422


def test_hedge_years_bounds_reject_out_of_range(client):
    r = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 0.0}, "years": 0},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 0.0}, "years": 100},
    )
    assert r2.status_code == 422
