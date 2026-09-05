"""API contract tests for the Hedge Lab route: POST /api/hedge ranks sized
hedge candidates against a beta_target objective. Cointegration is a
DIAGNOSTIC column only (Engineering Constraint 12) — ranking is strictly by
protection (ES reduction), never by cointegration. Serialization policy:
UTC ISO timestamps, NaN -> null, unknown symbol/empty candidates/bounds ->
structured 422, never a 500."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.fx import EcbFxProvider, sync_ecb_fx
from quantmind.risk.returns import historical_es, rolling_beta


def _write_metadata(store: BarStore, symbol: str, fields: dict) -> None:
    payload = dict(fields)
    if "con_id" not in payload:
        con_id = store.read_symbol_map().get(symbol)
        if con_id is not None:
            payload["con_id"] = con_id
    store.write_instrument_metadata(symbol, payload)


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
    for symbol in ("SPY", "QQQ", "IWM", "FLAT"):
        _write_metadata(store, symbol, {"currency": "USD", "exchange": "SMART"})
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


def test_hedge_long_short_target_beta_sizing_uses_book_gross(client):
    response = client.post(
        "/api/hedge",
        json={
            "book": [
                {"symbol": "SPY", "qty": 10},
                {"symbol": "QQQ", "qty": -5},
            ],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["IWM"],
            "years": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    candidate = body["candidates"][0]
    spy = _bars(seed=1)["close"].iloc[-252:]
    qqq = _beta_correlated_bars(
        _bars(seed=1)["close"], beta=0.8, noise_scale=0.002, seed=2
    )["close"].iloc[-252:]
    book_gross = abs(10 * spy.iloc[-1]) + abs(-5 * qqq.iloc[-1])
    signed_net = 10 * spy.iloc[-1] - 5 * qqq.iloc[-1]
    expected_notional = -body["book_beta"] * book_gross / candidate["beta"]
    net_value_notional = -body["book_beta"] * signed_net / candidate["beta"]

    assert body["book_value"] == pytest.approx(signed_net)
    assert candidate["hedge_notional"] == pytest.approx(expected_notional)
    assert candidate["hedge_notional"] != pytest.approx(net_value_notional)


def test_hedge_aligns_book_marks_before_valuation_sizing_and_as_of(tmp_path):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    qqq = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    ).iloc[:-5]
    iwm = _beta_correlated_bars(
        spy["close"], beta=0.5, noise_scale=0.003, seed=3
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", qqq, meta)
    store.write_bars(3, "1d", iwm, meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2, "IWM": 3})
    for symbol in ("SPY", "QQQ", "IWM"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    body = client.post(
        "/api/hedge",
        json={
            "book": [
                {"symbol": "SPY", "qty": 10},
                {"symbol": "QQQ", "qty": 5},
            ],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["IWM"],
            "years": 1,
        },
    ).json()

    aligned_date = qqq.index[-1]
    expected_value = 10 * spy.loc[aligned_date, "close"] + 5 * qqq.loc[aligned_date, "close"]
    expected_gross = abs(10 * spy.loc[aligned_date, "close"]) + abs(
        5 * qqq.loc[aligned_date, "close"]
    )
    candidate = body["candidates"][0]
    expected_notional = -body["book_beta"] * expected_gross / candidate["beta"]

    assert body["book_value"] == pytest.approx(expected_value)
    assert body["as_of"] == aligned_date.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert candidate["hedge_notional"] == pytest.approx(expected_notional)
    assert candidate["hedge_qty"] == pytest.approx(
        expected_notional / iwm.loc[aligned_date, "close"]
    )


def test_hedge_sizes_european_book_and_candidate_in_one_base_currency(tmp_path):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    candidate = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", spy.copy(), meta)
    store.write_bars(3, "1d", candidate, meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2, "QQQ": 3})
    _write_metadata(store, "SPY", {"currency": "USD"})
    _write_metadata(store, "IWDA", {"currency": "EUR"})
    _write_metadata(store, "QQQ", {"currency": "USD"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in spy.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    rows.extend(["USD,2026-09-04,1.2000", "GBP,2026-09-04,0.9000"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=date(2026, 9, 4),
        years=5,
        fetched_at="2026-09-04T17:00:00Z",
    )
    app = create_app(
        store=store,
        benchmark="SPY",
        api_token="testtoken",
        base_currency="GBP",
    )
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    body = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "IWDA", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    ).json()

    assert body["fx"]["base_currency"] == "GBP"
    assert body["book_value"] == pytest.approx(10 * spy["close"].iloc[-1] * 0.88)
    assert body["fx"]["source"] == "ECB"
    assert body["fx"]["as_of"] == "2026-07-24"
    assert body["fx"]["fetched_at"] == "2026-09-04T17:00:00Z"
    assert body["candidates"][0]["hedge_notional"] is not None


def test_hedge_reports_fx_provenance_when_only_the_candidate_needs_conversion(tmp_path):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    eur_candidate = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", eur_candidate, meta)
    store.write_symbol_map({"SPY": 1, "EXSA": 2})
    _write_metadata(store, "SPY", {"currency": "USD"})
    _write_metadata(store, "EXSA", {"currency": "EUR"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in spy.index:
        rows.append(f"USD,{timestamp.date().isoformat()},1.1000")
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR"},
        today=spy.index[-1].date(),
        years=5,
        fetched_at="2026-07-24T17:00:00Z",
    )
    app = create_app(
        store=store,
        benchmark="SPY",
        api_token="testtoken",
        base_currency="USD",
    )
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["EXSA"],
            "years": 1,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["fx"]["source"] == "ECB"
    assert response.json()["fx"]["as_of"] == "2026-07-24"
    assert response.json()["fx"]["fetched_at"] == "2026-07-24T17:00:00Z"


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
    assert body["skipped_candidates"] == []


def test_hedge_requested_candidate_already_in_book_is_named_422(client):
    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["SPY", "QQQ"],
        },
    )

    assert response.status_code == 422
    assert "SPY: candidate is already present in book" in response.json()["detail"]


def test_hedge_requested_candidate_without_currency_is_named_422(client):
    _write_metadata(client.app.state.store, "QQQ", {"currency": None})

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
        },
    )

    assert response.status_code == 422
    assert "QQQ: missing currency metadata" in response.json()["detail"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"con_id": 999, "currency": "USD"},
        {"currency": "USD"},
    ],
    ids=["mismatched-con-id", "missing-con-id"],
)
def test_hedge_rejects_candidate_metadata_without_the_mapped_contract_identity(
    client, metadata
):
    stored_metadata = client.app.state.store.read_all_instrument_metadata()
    stored_metadata["QQQ"] = metadata
    client.app.state.store.replace_instrument_metadata(stored_metadata)

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
        },
    )

    assert response.status_code == 422
    assert "QQQ" in response.json()["detail"]
    assert "contract identity" in response.json()["detail"]


def test_hedge_default_universe_reports_skipped_candidates(client):
    _write_metadata(client.app.state.store, "QQQ", {"currency": None})

    body = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    ).json()

    assert {candidate["symbol"] for candidate in body["candidates"]} == {"IWM", "FLAT"}
    assert body["skipped_candidates"] == [
        {"symbol": "QQQ", "reason": "missing currency metadata"}
    ]


def test_hedge_default_discovery_fills_budget_past_unusable_early_entries(
    tmp_path,
):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    candidate = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", candidate, meta)
    missing = [f"AAA_MISSING_{index:02d}" for index in range(10)]
    eligible = [f"ELIGIBLE_{index:02d}" for index in range(55)]
    store.write_symbol_map(
        {
            "SPY": 1,
            **{symbol: 100 + index for index, symbol in enumerate(missing)},
            **{symbol: 2 for symbol in eligible},
        }
    )
    _write_metadata(store, "SPY", {"currency": "USD"})
    for symbol in eligible:
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "years": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["n_candidates_evaluated"] == 50
    assert {
        candidate["symbol"]
        for candidate in body["skipped_candidates"]
        if candidate["reason"] == "missing currency metadata"
    } == set(missing)
    assert {
        candidate["symbol"]
        for candidate in body["skipped_candidates"]
        if "evaluation limit" in candidate["reason"]
    } == set(eligible[50:])


def test_hedge_default_discovery_names_candidates_omitted_by_scan_cap(tmp_path):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    candidate = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", candidate, meta)
    missing = [f"MISSING_{index:03d}" for index in range(201)]
    store.write_symbol_map(
        {
            "SPY": 1,
            **{symbol: 100 + index for index, symbol in enumerate(missing)},
        "ZZZ_LATE_ELIGIBLE": 2,
        }
    )
    _write_metadata(store, "SPY", {"currency": "USD"})
    _write_metadata(store, "ZZZ_LATE_ELIGIBLE", {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "years": 1,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "default candidate scan limit" in detail
    assert "2 additional cached candidates" in detail


def test_hedge_default_evaluation_is_independent_of_symbol_map_order(tmp_path):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    candidate = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", candidate, meta)
    candidates = [f"CANDIDATE_{index:02d}" for index in range(51)]
    _write_metadata(store, "SPY", {"con_id": 1, "currency": "USD"})
    for symbol in candidates:
        _write_metadata(store, symbol, {"con_id": 2, "currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )
    payload = {
        "book": [{"symbol": "SPY", "qty": 10}],
        "objective": {"kind": "beta_target", "value": 0.0},
        "years": 1,
    }

    store.write_symbol_map({"SPY": 1, **dict.fromkeys(candidates, 2)})
    forward = client.post("/api/hedge", json=payload)
    store.write_symbol_map(
        {"SPY": 1, **dict.fromkeys(reversed(candidates), 2)}
    )
    reverse = client.post("/api/hedge", json=payload)

    assert forward.status_code == reverse.status_code == 200
    assert forward.json() == reverse.json()


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


def test_hedge_candidate_universe_is_bounded(client):
    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": [f"CANDIDATE_{index}" for index in range(51)],
        },
    )

    assert response.status_code == 422
    assert any(error["type"] == "too_long" for error in response.json()["detail"])


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


def test_hedge_protection_not_inflated_by_large_hedge_notional(tmp_path):
    """I1 regression: es_after must be computed by overlaying the hedge on
    the ORIGINAL book (normalized by the original book's gross), never by
    re-normalizing a blended book+hedge portfolio by the NEW (hedge-inflated)
    gross. The old approach mechanically shrinks the hedge leg's weight
    whenever its notional is large relative to the book — which happens for
    any low-beta candidate, since sizing (hedge_qty ∝ 1/beta_cand) blows up
    as beta_cand shrinks. That let a low-beta, weakly-correlated candidate
    ("WEAK") look MORE protective than a well-correlated, sanely-sized one
    ("GOOD") purely because its huge notional dominated the blended gross and
    drowned out the book's own risk in the denominator — not because it
    actually reduces the book's tail risk. Pre-fix, this assertion fails
    (WEAK's protection > GOOD's); post-fix WEAK must not beat GOOD."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)  # SPY (book)
    store.write_bars(
        con_id=2,
        bar_size="1d",
        bars=_beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2),
        meta=meta,
    )  # GOOD: well-correlated, sanely-sized hedge
    store.write_bars(
        con_id=3,
        bar_size="1d",
        # Low beta (just above the 0.1 unusable floor) + tiny idiosyncratic
        # noise -> low own-volatility AND a huge required notional (sizing
        # is inversely proportional to beta_cand). This combination is what
        # exploited the old normalization: dominating the blended gross with
        # a low-vol instrument mechanically crushed es_after towards zero.
        bars=_beta_correlated_bars(spy_bars["close"], beta=0.15, noise_scale=0.0005, seed=3),
        meta=meta,
    )  # WEAK
    store.write_symbol_map({"SPY": 1, "GOOD": 2, "WEAK": 3})
    for symbol in ("SPY", "GOOD", "WEAK"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["GOOD", "WEAK"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    by_symbol = {c["symbol"]: c for c in body["candidates"]}
    good, weak = by_symbol["GOOD"], by_symbol["WEAK"]
    assert good["unusable"] is False
    assert weak["unusable"] is False

    # WEAK needs a much larger notional than GOOD to hit the same beta
    # target (sizing ∝ 1/beta_cand, and 0.15 << 0.8) — that's the setup for
    # the denominator-inflation exploit, not the bug itself.
    assert abs(weak["hedge_notional"]) > abs(good["hedge_notional"])

    # The bug: WEAK's protection must not exceed GOOD's purely because its
    # huge notional inflated (old code) or dominates (this is the honest
    # overlay check) the denominator. A weakly-correlated, low-beta hedge is
    # not a better hedge just because it's sized bigger.
    assert weak["protection"] is not None and good["protection"] is not None
    assert weak["protection"] <= good["protection"]


def test_hedge_candidates_share_one_es_window_when_one_has_recent_history(tmp_path):
    rng = np.random.default_rng(91)
    index = pd.bdate_range(end="2026-07-24", periods=300)
    spy_returns = rng.normal(0.0003, 0.009, len(index) - 1)
    spy_returns[-45] = -0.18
    spy_close = np.r_[100.0, 100.0 * np.cumprod(1 + spy_returns)]
    spy = pd.DataFrame(
        {
            "open": spy_close,
            "high": spy_close,
            "low": spy_close,
            "close": spy_close,
            "volume": 1000.0,
        },
        index=index,
    )
    candidate_returns = 0.8 * spy_returns + rng.normal(0, 0.001, len(spy_returns))
    # This candidate-only crash predates the recent candidate's listing.
    # Ranking the full and recent copies on different samples makes the
    # recent listing look safer even though their common-period behavior is
    # byte-for-byte identical.
    candidate_returns[50] = -0.30
    candidate_close = np.r_[80.0, 80.0 * np.cumprod(1 + candidate_returns)]
    full_candidate = pd.DataFrame(
        {
            "open": candidate_close,
            "high": candidate_close,
            "low": candidate_close,
            "close": candidate_close,
            "volume": 1000.0,
        },
        index=index,
    )
    recent_candidate = full_candidate.iloc[-220:].copy()

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", full_candidate, meta)
    store.write_bars(3, "1d", recent_candidate, meta)
    store.write_symbol_map({"SPY": 1, "FULL": 2, "RECENT": 3})
    for symbol in ("SPY", "FULL", "RECENT"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FULL", "RECENT"],
            "years": 5,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    by_symbol = {row["symbol"]: row for row in body["candidates"]}
    full = by_symbol["FULL"]
    recent = by_symbol["RECENT"]
    recent_returns = recent_candidate["close"].pct_change().dropna()
    common_book_returns = spy["close"].pct_change().dropna().loc[recent_returns.index]
    expected_es_before = historical_es(common_book_returns, confidence=0.975)

    assert full["unusable"] is False
    assert recent["unusable"] is False
    assert body["comparison_n_obs"] == len(recent_returns)
    assert body["comparison_as_of"] == f"{recent_returns.index[-1].isoformat()}Z"
    assert full["es_before"] == pytest.approx(expected_es_before)
    assert recent["es_before"] == pytest.approx(expected_es_before)
    assert full["es_after"] == pytest.approx(recent["es_after"])
    assert full["protection"] == pytest.approx(recent["protection"])


def test_hedge_builds_every_candidate_return_on_one_cross_calendar_grid(tmp_path):
    index = pd.bdate_range(end="2026-07-24", periods=601)
    rng = np.random.default_rng(707)
    close = pd.Series(
        100.0 * np.cumprod(1 + rng.normal(0.0002, 0.01, len(index))),
        index=index,
    )

    def bars(series):
        return pd.DataFrame(
            {
                "open": series,
                "high": series,
                "low": series,
                "close": series,
                "volume": 1000.0,
            },
            index=series.index,
        )

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", bars(close), meta)
    store.write_bars(2, "1d", bars(close.copy()), meta)
    store.write_bars(3, "1d", bars(close.copy()), meta)
    store.write_bars(4, "1d", bars(close.iloc[::2]), meta)
    store.write_symbol_map({"BENCH": 1, "BOOK": 2, "FULL": 3, "GAPS": 4})
    for symbol in ("BENCH", "BOOK", "FULL", "GAPS"):
        _write_metadata(store, symbol, {"currency": "USD"})
    client = TestClient(
        create_app(store=store, benchmark="BENCH", api_token="testtoken"),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "BOOK", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FULL", "GAPS"],
            "years": 5,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    by_symbol = {row["symbol"]: row for row in body["candidates"]}
    assert body["book_beta"] == pytest.approx(1.0, abs=1e-9)
    assert body["comparison_book_beta"] == pytest.approx(1.0, abs=1e-9)
    assert body["comparison_n_obs"] == len(close.iloc[::2]) - 1
    assert by_symbol["FULL"]["beta"] == pytest.approx(1.0, abs=1e-9)
    assert by_symbol["GAPS"]["beta"] == pytest.approx(1.0, abs=1e-9)
    assert by_symbol["FULL"]["es_after"] == pytest.approx(
        by_symbol["GAPS"]["es_after"], abs=1e-12
    )
    assert by_symbol["FULL"]["protection"] == pytest.approx(
        by_symbol["GAPS"]["protection"], abs=1e-12
    )
    assert by_symbol["FULL"]["hedge_qty"] == pytest.approx(-10.0, abs=1e-9)
    assert by_symbol["FULL"]["residual_beta"] == pytest.approx(0.0, abs=1e-9)


def test_hedge_sizes_with_the_book_beta_from_the_common_candidate_calendar(tmp_path):
    index = pd.bdate_range(end="2026-09-04", periods=521)
    rng = np.random.default_rng(42)
    benchmark_returns = rng.normal(0.0, 0.01, len(index) - 1)
    book_beta_regime = np.r_[np.full(460, 0.2), np.full(60, 2.0)]
    benchmark_close = pd.Series(
        np.r_[100.0, 100.0 * np.cumprod(1 + benchmark_returns)], index=index
    )
    book_close = pd.Series(
        np.r_[
            100.0,
            100.0 * np.cumprod(1 + book_beta_regime * benchmark_returns),
        ],
        index=index,
    )

    def bars(series):
        return pd.DataFrame(
            {
                "open": series,
                "high": series,
                "low": series,
                "close": series,
                "volume": 1000.0,
            },
            index=series.index,
        )

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-09-04")
    series_by_symbol = {
        "BENCH": benchmark_close,
        "BOOK": book_close,
        "FULL": book_close.copy(),
        "GAPS": benchmark_close.iloc[::2],
    }
    store.write_symbol_map(
        {symbol: con_id for con_id, symbol in enumerate(series_by_symbol, 1)}
    )
    for con_id, (symbol, series) in enumerate(series_by_symbol.items(), 1):
        store.write_bars(con_id, "1d", bars(series), meta)
        _write_metadata(store, symbol, {"con_id": con_id, "currency": "USD"})
    client = TestClient(
        create_app(store=store, benchmark="BENCH", api_token="testtoken"),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "BOOK", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FULL", "GAPS"],
            "years": 5,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    full = next(row for row in body["candidates"] if row["symbol"] == "FULL")
    assert body["book_beta"] == pytest.approx(2.0, abs=1e-9)
    assert body["comparison_book_beta"] == pytest.approx(full["beta"], abs=1e-9)
    assert full["hedge_qty"] == pytest.approx(-10.0, abs=1e-9)
    assert full["residual_beta"] == pytest.approx(0.0, abs=1e-9)


def test_incompatible_candidate_cohort_error_is_independent_of_request_order(tmp_path):
    spy = _bars(n=450, seed=101)
    early_full = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.001, seed=102
    )
    late = _beta_correlated_bars(
        spy["close"], beta=0.7, noise_scale=0.001, seed=103
    ).iloc[-300:]
    early_index = spy.index[:300].append(spy.index[-1:])
    early = early_full.loc[early_index]

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", early, meta)
    store.write_bars(3, "1d", late, meta)
    store.write_symbol_map({"SPY": 1, "EARLY": 2, "LATE": 3})
    for symbol in ("SPY", "EARLY", "LATE"):
        _write_metadata(store, symbol, {"currency": "USD"})
    client = TestClient(
        create_app(store=store, benchmark="SPY", api_token="testtoken"),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    payload = {
        "book": [{"symbol": "SPY", "qty": 10}],
        "objective": {"kind": "beta_target", "value": 0.0},
        "years": 5,
    }
    forward = client.post(
        "/api/hedge", json={**payload, "candidates": ["EARLY", "LATE"]}
    )
    reverse = client.post(
        "/api/hedge", json={**payload, "candidates": ["LATE", "EARLY"]}
    )

    assert forward.status_code == reverse.status_code == 422
    assert forward.json() == reverse.json()
    detail = forward.json()["detail"]
    assert "EARLY, LATE" in detail
    assert "collectively" in detail

    store.write_symbol_map({"SPY": 1, "EARLY": 2, "LATE": 3})
    default_forward = client.post("/api/hedge", json=payload)
    store.write_symbol_map({"SPY": 1, "LATE": 3, "EARLY": 2})
    default_reverse = client.post("/api/hedge", json=payload)

    assert default_forward.status_code == default_reverse.status_code == 200
    first = default_forward.json()
    second = default_reverse.json()
    assert [row["symbol"] for row in first["candidates"]] == [
        row["symbol"] for row in second["candidates"]
    ]
    assert first["skipped_candidates"] == second["skipped_candidates"]
    assert first["comparison_n_obs"] == second["comparison_n_obs"]
    assert first["comparison_as_of"] == second["comparison_as_of"]


def test_hedge_rejects_a_stale_requested_candidate_relative_to_book_as_of(
    tmp_path,
):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    stale = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    ).iloc[:-10]
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", stale, meta)
    store.write_symbol_map({"SPY": 1, "STALE": 2})
    for symbol in ("SPY", "STALE"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["STALE"],
            "years": 5,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "STALE" in detail
    assert "stale relative to book as-of" in detail
    assert "max 3" in detail


def test_hedge_rejects_requested_candidate_without_minimum_comparison_history(
    tmp_path,
):
    store = BarStore(tmp_path)
    spy = _bars(seed=1)
    short = _beta_correlated_bars(
        spy["close"], beta=0.8, noise_scale=0.002, seed=2
    ).iloc[-150:]
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy, meta)
    store.write_bars(2, "1d", short, meta)
    store.write_symbol_map({"SPY": 1, "SHORT": 2})
    for symbol in ("SPY", "SHORT"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["SHORT"],
            "years": 5,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "SHORT" in detail
    assert "insufficient common comparison history" in detail
    assert "at least 200" in detail


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


def test_hedge_response_has_no_cointegration_column(client):
    # Pre-wave-3 consolidation pass (TODOS.md): cointegration's home is Lab's
    # pair pipeline now, never the Hedge Lab response.
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
    assert "coint_pvalue" not in r.json()["candidates"][0]


def test_hedge_nonfinite_book_last_close_is_422_naming_the_symbol(tmp_path):
    # Aligned with routers/whatif.py's identical guard (pre-wave-3
    # consolidation pass, TODOS.md): a NaN last close on a BOOK leg must
    # never silently propagate into book_beta/es_before/protection.
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    bad_bars = _bars(seed=2)
    bad_bars.loc[bad_bars.index[-1], "close"] = np.nan
    store.write_bars(con_id=2, bar_size="1d", bars=bad_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "QQQ", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["SPY"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]


def test_hedge_zero_gross_book_is_422(client):
    # Two offsetting legs in the same symbol net to zero market value.
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}, {"symbol": "SPY", "qty": -10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "zero gross" in r.json()["detail"]


# --- book_ref (wave-3 Task A1's book-flow spine): an alternative to inline
# `book`, resolved via routers/book.py's pinned snapshots. ---


def test_hedge_book_ref_resolves_to_the_same_result_as_inline_book(client):
    pinned = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]}).json()

    r_ref = client.post(
        "/api/hedge",
        json={
            "book_ref": pinned["snapshot_id"],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    r_inline = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r_ref.status_code == r_inline.status_code == 200
    assert r_ref.json() == r_inline.json()


def test_hedge_rejects_live_book_from_a_different_account(client):
    from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond
    from quantmind.portfolio import Portfolio, Position

    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, currency="USD"),
        ),
        as_of="2026-09-04T12:00:00Z",
    )
    pinned = _pin_and_respond(
        client.app.state.store,
        portfolio,
        portfolio.as_of,
        source="live_ibkr",
        account_fingerprint=_account_fingerprint("DU_ACCOUNT_A"),
        broker_mode="paper",
    )
    client.app.state.broker_account_id = "DU_ACCOUNT_B"
    client.app.state.broker_mode = "paper"

    response = client.post(
        "/api/hedge",
        json={
            "book_ref": pinned.snapshot_id,
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )

    assert response.status_code == 409
    assert "account" in response.json()["detail"]


def test_hedge_refuses_inline_option_legs_until_contract_repricing_exists(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [
                {
                    "symbol": "SPY",
                    "qty": -1,
                    "strike": 100,
                    "expiry": "20260918",
                    "right": "C",
                    "multiplier": 100,
                }
            ],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "cannot value option" in r.json()["detail"].lower()


def test_hedge_refuses_option_legs_resolved_from_book_ref(client):
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": -1,
                    "strike": 100,
                    "expiry": "20260918",
                    "right": "P",
                    "multiplier": 100,
                }
            ]
        },
    ).json()

    r = client.post(
        "/api/hedge",
        json={
            "book_ref": pinned["snapshot_id"],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "cannot value option" in r.json()["detail"].lower()


def test_hedge_unknown_book_ref_is_422(client):
    r = client.post(
        "/api/hedge",
        json={"book_ref": "does-not-exist", "objective": {"kind": "beta_target", "value": 0.0}},
    )
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_hedge_both_book_and_book_ref_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "book_ref": "whatever",
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422


def test_hedge_neither_book_nor_book_ref_is_422(client):
    r = client.post("/api/hedge", json={"objective": {"kind": "beta_target", "value": 0.0}})
    assert r.status_code == 422


def test_leverage_reports_drawdown_headroom_and_diversification(client):
    r = client.post(
        "/api/leverage",
        json={"book": [{"symbol": "QQQ", "qty": 10}, {"symbol": "IWM", "qty": 10}], "drawdown_budget": 0.25},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["symbols"]) == {"QQQ", "IWM"}
    assert body["drawdown_budget"] == 0.25
    assert body["max_drawdown"] is not None and body["max_drawdown"] >= 0
    # headroom = budget / MDD; a positive number when the book drew down
    assert body["leverage_headroom"] is None or body["leverage_headroom"] > 0
    # DR >= 1 for a long-only two-name book (>= 1 minus float slack)
    assert body["diversification_ratio"] is not None and body["diversification_ratio"] >= 0.999
    assert "assumption-bound" in body["note"]
    assert body["n_obs"] >= 2


def test_leverage_normalizes_a_mixed_currency_book_and_reports_fx(tmp_path):
    store = BarStore(tmp_path)
    eur = _bars(seed=1)
    gbp = _bars(seed=2)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", eur, meta)
    store.write_bars(2, "1d", gbp, meta)
    store.write_symbol_map({"IWDA": 1, "VWRL": 2})
    _write_metadata(store, "IWDA", {"currency": "EUR"})
    _write_metadata(store, "VWRL", {"currency": "GBP"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in eur.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=eur.index[-1].date(),
        years=5,
        fetched_at="2026-07-24T17:00:00Z",
    )
    app = create_app(
        store=store,
        benchmark="SPY",
        api_token="testtoken",
        base_currency="USD",
    )
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/leverage",
        json={
            "book": [
                {"symbol": "IWDA", "qty": 10},
                {"symbol": "VWRL", "qty": 20},
            ],
            "years": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    expected_eur = 10 * eur["close"].iloc[-1] * 1.10
    expected_gbp = 20 * gbp["close"].iloc[-1] * (1.10 / 0.88)
    assert body["book_value"] == pytest.approx(expected_eur + expected_gbp)
    assert body["gross"] == pytest.approx(abs(expected_eur) + abs(expected_gbp))
    assert body["fx"]["base_currency"] == "USD"
    assert body["fx"]["source"] == "ECB"
    assert body["fx"]["as_of"] == "2026-07-24"
    assert body["max_drawdown"] is not None


def test_leverage_refuses_a_non_base_book_without_dated_fx(tmp_path):
    store = BarStore(tmp_path)
    bars = _bars(seed=1)
    store.write_bars(
        1,
        "1d",
        bars,
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 1})
    _write_metadata(store, "IWDA", {"currency": "EUR"})
    app = create_app(
        store=store,
        benchmark="SPY",
        api_token="testtoken",
        base_currency="USD",
    )
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/leverage", json={"book": [{"symbol": "IWDA", "qty": 10}]}
    )

    assert response.status_code == 422
    assert "FX normalization" in response.json()["detail"]


def test_leverage_single_name_has_no_diversification_ratio(client):
    r = client.post("/api/leverage", json={"book": [{"symbol": "QQQ", "qty": 10}]})
    assert r.status_code == 200, r.text
    assert r.json()["diversification_ratio"] is None  # needs >= 2 instruments


def test_leverage_refuses_option_legs_until_contract_repricing_exists(client):
    r = client.post(
        "/api/leverage",
        json={
            "book": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 100,
                    "expiry": "20260918",
                    "right": "C",
                    "multiplier": 100,
                }
            ]
        },
    )
    assert r.status_code == 422
    assert "cannot value option" in r.json()["detail"].lower()


def test_leverage_unknown_symbol_is_422(client):
    r = client.post("/api/leverage", json={"book": [{"symbol": "NOPE", "qty": 1}]})
    assert r.status_code == 422


def test_leverage_requires_book_xor_book_ref(client):
    r = client.post("/api/leverage", json={"drawdown_budget": 0.25})
    assert r.status_code == 422

def test_leverage_nonfinite_book_last_close_is_422_naming_the_symbol(tmp_path):
    # /leverage must not silently return a 200 of nulls when a book leg has a
    # NaN last close (NaN gross slips past `gross <= 0`) — mirror /hedge's guard.
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    bad_bars = _bars(seed=2)
    bad_bars.loc[bad_bars.index[-1], "close"] = np.nan
    store.write_bars(con_id=2, bar_size="1d", bars=bad_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = c.post("/api/leverage", json={"book": [{"symbol": "QQQ", "qty": 10}], "years": 1})
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]
