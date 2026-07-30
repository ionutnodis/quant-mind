"""API contract tests for the options domain (Task A3):
GET /api/options/{underlier}/chain (cached, staleness-stamped, never-500) and
POST /api/options/book-greeks (thin composition over exposure/book_greeks.py
+ risk/options.py, IV/spot sourced from the store — never a live IB call).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.risk.options import bs_greeks


def _bars(n=10, price=452.0):
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def store(tmp_path):
    s = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    s.write_bars(con_id=1, bar_size="1d", bars=_bars(price=452.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_bars(price=380.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "QQQ": 2})
    return s


@pytest.fixture
def client(store):
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", base_currency="USD")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _chain_df():
    return pd.DataFrame(
        {
            "expiry": ["20260918", "20260918", "20260918", "20260918"],
            "strike": [440.0, 440.0, 460.0, 460.0],
            "right": ["C", "P", "C", "P"],
            "con_id": [1001, 1002, 1003, 1004],
            "bid": [10.1, 8.2, 3.5, 14.3],
            "ask": [10.3, 8.4, 3.7, 14.5],
            "iv": [0.18, 0.20, 0.19, 0.21],
            "delta": [0.55, -0.45, 0.40, -0.60],
            "multiplier": [100.0, 100.0, 100.0, 100.0],
        }
    )


def _write_spy_chain(store, as_of="2026-07-24"):
    OptionsStore(store.root).write_chain("SPY", _chain_df(), OptionsSnapshotMeta(as_of=as_of, spot=452.0))


# --- GET /api/options/{underlier}/chain ---


def test_chain_missing_underlier_is_structured_empty_not_error(client):
    r = client.get("/api/options/SPY/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["missing"] is True
    assert body["quotes"] == []
    assert body["smile"] == []
    assert body["stale"] is True


def test_chain_present_returns_quotes_and_smile(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    r = client.get("/api/options/SPY/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["missing"] is False
    assert body["spot"] == pytest.approx(452.0)
    assert len(body["quotes"]) == 4
    assert body["stale"] is False
    # one expiry, two strikes -> smile has 1 expiry group with 2 strike points
    assert len(body["smile"]) == 1
    assert body["smile"][0]["expiry"] == "20260918"
    strikes = sorted(p["strike"] for p in body["smile"][0]["points"])
    assert strikes == [440.0, 460.0]
    # smile IV at 440 averages call (0.18) and put (0.20)
    point_440 = next(p for p in body["smile"][0]["points"] if p["strike"] == 440.0)
    assert point_440["iv"] == pytest.approx((0.18 + 0.20) / 2)


def test_chain_stale_when_as_of_older_than_threshold(client, store):
    old = str(date.today() - timedelta(days=10))
    _write_spy_chain(store, as_of=old)
    r = client.get("/api/options/SPY/chain")
    assert r.json()["stale"] is True


# --- POST /api/options/book-greeks ---


def test_book_greeks_stock_only_position(client):
    r = client.post("/api/options/book-greeks", json={"positions": [{"symbol": "SPY", "qty": 100}]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["underlyings"]) == 1
    row = body["underlyings"][0]
    assert row["underlier"] == "SPY"
    assert row["delta"] == pytest.approx(100.0)
    assert row["dollar_delta"] == pytest.approx(100.0 * 452.0)
    assert row["spy_equivalent_notional"] is None


def test_book_greeks_option_leg_pulls_iv_from_cached_chain(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": "20260918", "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 200
    row = r.json()["underlyings"][0]

    expiry_years = (date(2026, 9, 18) - date.today()).days / 365.25
    expected = bs_greeks(452.0, 440.0, expiry_years, 0.0, 0.18, True)
    assert row["delta"] == pytest.approx(2 * 100 * expected.delta, rel=1e-4)
    assert row["gamma"] == pytest.approx(2 * 100 * expected.gamma, rel=1e-4)


def test_book_greeks_unknown_option_leg_is_422_not_500(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 1, "strike": 999.0, "expiry": "20260918", "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 422


def test_book_greeks_option_leg_without_cached_chain_is_422(client):
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 1, "strike": 440.0, "expiry": "20260918", "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 422


def test_book_greeks_requires_exactly_one_of_positions_or_book_ref(client):
    r = client.post("/api/options/book-greeks", json={})
    assert r.status_code == 422
    r2 = client.post(
        "/api/options/book-greeks",
        json={"positions": [{"symbol": "SPY", "qty": 1}], "book_ref": "deadbeef"},
    )
    assert r2.status_code == 422


def test_book_greeks_betas_populate_spy_equivalent_notional(client):
    payload = {"positions": [{"symbol": "QQQ", "qty": 10}], "betas": {"QQQ": 1.1}}
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 200
    row = r.json()["underlyings"][0]
    assert row["dollar_delta"] == pytest.approx(10 * 380.0)
    assert row["spy_equivalent_notional"] == pytest.approx(10 * 380.0 * 1.1)


def test_book_greeks_via_book_ref_round_trip(client):
    pin = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 50}]})
    assert pin.status_code == 200
    snapshot_id = pin.json()["snapshot_id"]

    r = client.post("/api/options/book-greeks", json={"book_ref": snapshot_id})
    assert r.status_code == 200
    row = r.json()["underlyings"][0]
    assert row["underlier"] == "SPY"
    assert row["delta"] == pytest.approx(50.0)


def test_book_greeks_unknown_book_ref_is_422(client):
    r = client.post("/api/options/book-greeks", json={"book_ref": "not-a-real-ref"})
    assert r.status_code == 422


def test_book_greeks_unknown_symbol_is_422(client):
    r = client.post("/api/options/book-greeks", json={"positions": [{"symbol": "MSFT", "qty": 1}]})
    assert r.status_code == 422


# --- final-fix-wave (2026-07-25): option legs must round-trip through pinned
# books (finding 1 — the blocker: previously priced as bare shares), and
# book_ref must be a validated 12-hex-char id, never an unvalidated path
# (finding 2). ---


def test_book_greeks_via_book_ref_option_leg_matches_inline(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    positions = [{"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": "20260918", "right": "C"}]

    inline = client.post("/api/options/book-greeks", json={"positions": positions})
    assert inline.status_code == 200
    inline_row = inline.json()["underlyings"][0]

    pinned = client.post("/api/book/pin", json={"positions": positions})
    assert pinned.status_code == 200
    snapshot_id = pinned.json()["snapshot_id"]

    via_ref = client.post("/api/options/book-greeks", json={"book_ref": snapshot_id})
    assert via_ref.status_code == 200
    ref_row = via_ref.json()["underlyings"][0]

    # Regression guard: before the fix, book_ref resolution dropped
    # strike/expiry/right, so this leg priced as 2 bare SPY shares instead
    # of an option (delta ~2.0 instead of ~2*100*bs_delta).
    assert ref_row["delta"] == pytest.approx(inline_row["delta"])
    assert ref_row["gamma"] == pytest.approx(inline_row["gamma"])
    assert ref_row["dollar_delta"] == pytest.approx(inline_row["dollar_delta"])


def test_book_greeks_book_ref_with_invalid_format_is_422_not_500(client, store):
    # {"book_ref": "../instruments"} would resolve to {root}/instruments.json
    # (A2's instrument-metadata store, a dict with no "positions" key) if the
    # ref weren't format-validated first -> KeyError -> 500 (finding 2).
    store.write_instrument_metadata("SPY", {"exchange": "ARCA"})
    r = client.post("/api/options/book-greeks", json={"book_ref": "../instruments"})
    assert r.status_code == 422


def test_book_greeks_book_ref_corrupted_snapshot_is_422_not_500(client, store):
    from quantmind.api.routers.book import _books_dir

    bad_id = "abcdef012345"
    (_books_dir(store) / f"{bad_id}.json").write_text("{not valid json")
    r = client.post("/api/options/book-greeks", json={"book_ref": bad_id})
    assert r.status_code == 422


def test_book_greeks_expiry_accepts_iso_form(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": "2026-09-18", "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 200
