"""API contract tests for the book-flow spine (wave-3 Task A1):
POST /api/book/pin, GET /api/book/{id}, GET /api/book/current. A BookSnapshot
pins either the live broker's book or a posted position list as immutable
JSON under `{store.root}/books/{snapshot_id}.json` — never through
datastore/store.py (A2 owns that file this wave).

Serialization policy: UTC ISO Z timestamps, unknown book_ref/symbols ->
structured 422, never a 500 (repo-wide policy, pattern: routers/whatif.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.portfolio import Portfolio, Position


def _bars(n=300, seed=1, price=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * price
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


class FakeBroker:
    def __init__(self, portfolio: Portfolio):
        self._portfolio = portfolio

    async def get_portfolio(self) -> Portfolio:
        return self._portfolio


@pytest.fixture
def store(tmp_path) -> BarStore:
    s = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    s.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)  # SPY
    s.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2), meta=meta)  # QQQ
    s.write_symbol_map({"SPY": 1, "QQQ": 2})
    return s


def _client(store: BarStore, broker=None) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", broker=broker)
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_pin_explicit_positions_persists_and_echoes(store):
    client = _client(store)
    r = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]})
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_id"]
    assert body["valuation_ts"].endswith("Z")
    assert body["base_currency"] == "USD"
    assert len(body["positions"]) == 1
    pos = body["positions"][0]
    assert pos["symbol"] == "SPY"
    assert pos["qty"] == 10
    assert pos["con_id"] == 1
    assert pos["sec_type"] == "STK"
    assert pos["multiplier"] == 1.0


def test_pin_unknown_symbol_is_422(store):
    client = _client(store)
    r = client.post("/api/book/pin", json={"positions": [{"symbol": "NOPE", "qty": 1}]})
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_pin_no_broker_no_positions_is_empty_book(store):
    client = _client(store, broker=None)
    r = client.post("/api/book/pin", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []


def test_pin_option_leg_defaults_multiplier_100(store):
    client = _client(store)
    r = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 2, "strike": 450.0, "expiry": "2026-09-18", "right": "C"}]},
    )
    assert r.status_code == 200
    pos = r.json()["positions"][0]
    assert pos["sec_type"] == "OPT"
    assert pos["multiplier"] == 100.0


def test_pin_stock_leg_without_multiplier_defaults_to_one(store):
    # Regression guard: PositionIn.multiplier has no baked-in default of 100
    # (that would silently 100x a plain equity leg) — only an option leg
    # (right set) without an explicit multiplier defaults to 100.
    client = _client(store)
    r = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 2}]})
    assert r.status_code == 200
    assert r.json()["positions"][0]["multiplier"] == 1.0


def test_get_book_roundtrips_a_pinned_snapshot(store):
    client = _client(store)
    pinned = client.post("/api/book/pin", json={"positions": [{"symbol": "QQQ", "qty": 5}]}).json()
    r = client.get(f"/api/book/{pinned['snapshot_id']}")
    assert r.status_code == 200
    assert r.json() == pinned


def test_get_unknown_book_id_is_422(store):
    client = _client(store)
    r = client.get("/api/book/does-not-exist")
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_current_book_with_no_broker_is_empty_and_auto_pinned(store):
    client = _client(store, broker=None)
    r = client.get("/api/book/current")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    # auto-pinned: the same id resolves via GET /api/book/{id}.
    r2 = client.get(f"/api/book/{body['snapshot_id']}")
    assert r2.status_code == 200
    assert r2.json() == body


def test_current_book_with_broker_reads_live_positions_and_auto_pins(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=7, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.get("/api/book/current")
    assert r.status_code == 200
    body = r.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "SPY"
    assert body["positions"][0]["qty"] == 7

    r2 = client.get(f"/api/book/{body['snapshot_id']}")
    assert r2.status_code == 200
    assert r2.json() == body


def test_repinning_identical_content_at_the_same_valuation_ts_is_idempotent(store):
    # Content-hashed ids (quantmind.core.snapshot.BookSnapshot, unit-tested in
    # tests/test_snapshot.py for determinism): pinning the identical
    # portfolio at the identical valuation_ts twice writes to the same file
    # rather than racing/duplicating. Fixed valuation_ts here (rather than
    # two real HTTP calls) avoids a real-clock second-boundary flake while
    # still exercising book.py's own write_book/read_book helpers.
    from quantmind.api.routers.book import read_book, write_book
    from quantmind.core.snapshot import BookSnapshot

    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24T00:00:00Z",
    )
    snap1 = BookSnapshot.create(portfolio, valuation_ts="2026-07-24T00:00:00Z", base_currency="USD")
    snap2 = BookSnapshot.create(portfolio, valuation_ts="2026-07-24T00:00:00Z", base_currency="USD")
    assert snap1.snapshot_id == snap2.snapshot_id

    write_book(store, snap1)
    write_book(store, snap2)  # idempotent rewrite of the same file, never a race
    payload = read_book(store, snap1.snapshot_id)
    assert payload["positions"][0]["symbol"] == "SPY"
