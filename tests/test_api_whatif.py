"""API contract tests for POST /api/whatif: hypothetical-book risk composition
reusing quantmind.risk.returns/montecarlo only (Global Constraints — routers
are thin composition, no math beyond that). Hypothetical books ARE the user's
book for color purposes (wave-2 constraints addendum) — this file only
asserts the numbers; amber rendering is web/src/pages/WhatIf.tsx's job.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or insufficient overlap -> structured 422, never a 500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.fx import EcbFxProvider, sync_ecb_fx


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


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    # QQQ tracks SPY tick-for-tick in this fixture: a two-symbol book that's
    # economically ~100% index exposure has a hand-checkable beta ~= 1,
    # regardless of the 60/40 qty split between the two legs.
    store.write_bars(con_id=2, bar_size="1d", bars=spy_bars.copy(), meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    for symbol in ("SPY", "QQQ"):
        _write_metadata(store, symbol, {"currency": "USD", "exchange": "SMART"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _payload(**overrides):
    body = {
        "positions": [{"symbol": "SPY", "qty": 60}, {"symbol": "QQQ", "qty": 40}],
        "years": 1,
        "mc": {"horizon": 21, "n_paths": 2000, "seed": 7},
    }
    body.update(overrides)
    return body


def test_whatif_two_symbol_book_beta_is_near_one(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["beta"] == pytest.approx(1.0, abs=1e-6)
    assert body["es_975"] is not None and body["es_975"] >= 0
    assert body["ann_vol"] is not None and body["ann_vol"] >= 0
    assert body["benchmark"]["symbol"] == "SPY"
    assert body["benchmark"]["es_975"] is not None
    assert body["benchmark"]["ann_vol"] is not None
    weights = {w["symbol"]: w for w in body["weights"]}
    assert weights["SPY"]["weight"] == pytest.approx(0.6)
    assert weights["QQQ"]["weight"] == pytest.approx(0.4)
    assert body["as_of"] and body["as_of"].endswith("Z")


def test_whatif_weights_use_the_reported_common_as_of_mark(tmp_path):
    store = BarStore(tmp_path)
    spy_bars = _bars(seed=9)
    qqq_bars = spy_bars.iloc[:-1].copy()
    # A newer SPY-only observation must not leak into weights for a result
    # whose reported as-of is the prior common trading date.
    spy_bars.loc[spy_bars.index[-1], "close"] *= 10
    for column in ("open", "high", "low"):
        spy_bars.loc[spy_bars.index[-1], column] = spy_bars.loc[
            spy_bars.index[-1], "close"
        ]
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", spy_bars, meta)
    store.write_bars(2, "1d", qqq_bars, meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    for symbol in ("SPY", "QQQ"):
        _write_metadata(store, symbol, {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/whatif",
        json=_payload(
            positions=[
                {"symbol": "SPY", "qty": 1},
                {"symbol": "QQQ", "qty": 1},
            ]
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    weights = {row["symbol"]: row for row in body["weights"]}
    expected_as_of = qqq_bars.index[-1]
    assert body["as_of"] == f"{expected_as_of.isoformat()}Z"
    assert weights["SPY"]["price"] == pytest.approx(
        float(spy_bars.loc[expected_as_of, "close"])
    )
    assert weights["QQQ"]["price"] == pytest.approx(
        float(qqq_bars.loc[expected_as_of, "close"])
    )
    assert weights["SPY"]["weight"] == pytest.approx(0.5)
    assert weights["QQQ"]["weight"] == pytest.approx(0.5)


def test_whatif_uses_dated_base_currency_prices_for_european_book(tmp_path):
    store = BarStore(tmp_path)
    bars = _bars(seed=1)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2})
    _write_metadata(store, "SPY", {"currency": "USD", "exchange": "ARCA"})
    _write_metadata(store, "IWDA", {"currency": "EUR", "exchange": "AEB"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in bars.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=bars.index[-1].date(),
        years=5,
        fetched_at="2026-07-24T17:00:00Z",
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

    response = c.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "IWDA", "qty": 1}, {"symbol": "SPY", "qty": 1}],
            "years": 1,
            "mc": {"horizon": 5, "n_paths": 100, "seed": 1},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fx"]["base_currency"] == "GBP"
    weights = {item["symbol"]: item for item in body["weights"]}
    # EUR local 100 -> GBP 88; USD local 100 -> GBP 80.
    assert weights["IWDA"]["price"] / weights["SPY"]["price"] == pytest.approx(1.1)
    assert weights["IWDA"]["weight"] == pytest.approx(88 / 168)
    assert body["beta"] == pytest.approx(1.0, abs=1e-6)


def test_whatif_unknown_symbol_is_422_naming_it(client):
    r = client.post(
        "/api/whatif",
        json=_payload(positions=[{"symbol": "SPY", "qty": 1}, {"symbol": "NOPE", "qty": 1}]),
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_whatif_qty_zero_is_422(client):
    r = client.post("/api/whatif", json=_payload(positions=[{"symbol": "SPY", "qty": 0}]))
    assert r.status_code == 422


def test_whatif_too_many_positions_is_422(client):
    positions = [{"symbol": "SPY", "qty": 1} for _ in range(51)]
    r = client.post("/api/whatif", json=_payload(positions=positions))
    assert r.status_code == 422


def test_whatif_empty_positions_is_422(client):
    r = client.post("/api/whatif", json=_payload(positions=[]))
    assert r.status_code == 422


def test_whatif_years_bounds_reject_out_of_range(client):
    r = client.post("/api/whatif", json=_payload(years=0))
    assert r.status_code == 422
    r2 = client.post("/api/whatif", json=_payload(years=100))
    assert r2.status_code == 422


def test_whatif_mc_bounds_reject_resource_exhaustion(client):
    r = client.post(
        "/api/whatif", json=_payload(mc={"horizon": 21, "n_paths": 10_000_000, "seed": 1})
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/whatif", json=_payload(mc={"horizon": 100_000, "n_paths": 100, "seed": 1})
    )
    assert r2.status_code == 422


def test_whatif_mc_seeded_run_is_reproducible(client):
    payload = _payload()
    r1 = client.post("/api/whatif", json=payload)
    r2 = client.post("/api/whatif", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_whatif_returns_mc_histogram_and_percentiles(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    body = r.json()
    hist = body["mc"]["histogram"]
    assert len(hist["bin_edges"]) == len(hist["counts"]) + 1
    assert len(hist["counts"]) <= 60
    assert sum(hist["counts"]) == 2000 - body["mc"]["n_nonfinite"]
    assert body["mc"]["p5"] <= body["mc"]["p50"] <= body["mc"]["p95"]


def test_whatif_insufficient_overlap_is_422(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    short_bars = _bars(n=10, seed=3)
    store.write_bars(con_id=1, bar_size="1d", bars=short_bars, meta=meta)
    store.write_symbol_map({"SPY": 1})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    short_client = TestClient(
        app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"}
    )
    r = short_client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 1}],
            "years": 1,
            "mc": {"horizon": 5, "n_paths": 100},
        },
    )
    assert r.status_code == 422
    assert "detail" in r.json()


def test_whatif_nonfinite_last_close_is_422_naming_the_symbol(tmp_path):
    # Corrupted/partial sync data: a NaN last close makes the leg unpriceable.
    # Unlike GET /api/portfolio (display of broker truth, where a leg degrades
    # to null fields), What-If's weights ARE the risk computation — silently
    # dropping the leg would compute risk for a different book than the one
    # the user built. So: structured 422 naming the symbol, never a NaN
    # leaking into the JSON (binding NaN/Inf -> null / never-crash policy).
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    bad_bars = _bars(seed=2)
    bad_bars.loc[bad_bars.index[-1], "close"] = np.nan
    store.write_bars(con_id=2, bar_size="1d", bars=bad_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    _write_metadata(store, "SPY", {"currency": "USD"})
    _write_metadata(store, "QQQ", {"currency": "USD"})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = c.post("/api/whatif", json=_payload())
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]

    # A book that avoids the corrupted symbol still computes fine.
    r2 = c.post("/api/whatif", json=_payload(positions=[{"symbol": "SPY", "qty": 10}]))
    assert r2.status_code == 200


def test_whatif_single_position_defaults_mc(client):
    r = client.post(
        "/api/whatif",
        json={"positions": [{"symbol": "SPY", "qty": 10}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["weights"][0]["weight"] == pytest.approx(1.0)
    assert body["mc"]["histogram"]["counts"]


# --- book_ref (wave-3 Task A1's book-flow spine): an alternative to inline
# `positions`, resolved via routers/book.py's pinned snapshots. ---


def test_whatif_book_ref_resolves_to_the_same_result_as_inline_positions(client):
    pinned = client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 60}, {"symbol": "QQQ", "qty": 40}]}
    ).json()

    r_ref = client.post("/api/whatif", json={"book_ref": pinned["snapshot_id"], "years": 1, "mc": {"horizon": 21, "n_paths": 2000, "seed": 7}})
    r_inline = client.post("/api/whatif", json=_payload())
    assert r_ref.status_code == r_inline.status_code == 200
    assert r_ref.json() == r_inline.json()


def test_whatif_rejects_live_book_from_a_different_account(client):
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
        "/api/whatif", json={"book_ref": pinned.snapshot_id, "years": 1}
    )

    assert response.status_code == 409
    assert "account" in response.json()["detail"]


def test_whatif_refuses_inline_option_legs_until_contract_repricing_exists(client):
    r = client.post(
        "/api/whatif",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 100,
                    "expiry": "20260918",
                    "right": "C",
                    "multiplier": 100,
                }
            ],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "cannot value option" in r.json()["detail"].lower()


def test_whatif_refuses_option_legs_resolved_from_book_ref(client):
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 100,
                    "expiry": "20260918",
                    "right": "P",
                    "multiplier": 100,
                }
            ]
        },
    ).json()

    r = client.post("/api/whatif", json={"book_ref": pinned["snapshot_id"], "years": 1})
    assert r.status_code == 422
    assert "cannot value option" in r.json()["detail"].lower()


def test_whatif_unknown_book_ref_is_422(client):
    r = client.post("/api/whatif", json={"book_ref": "does-not-exist", "years": 1})
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_whatif_both_positions_and_book_ref_is_422(client):
    r = client.post("/api/whatif", json=_payload(book_ref="whatever"))
    assert r.status_code == 422


def test_whatif_neither_positions_nor_book_ref_is_422(client):
    r = client.post("/api/whatif", json={"years": 1})
    assert r.status_code == 422
