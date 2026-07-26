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


# --- final-fix-wave (2026-07-25): option legs must round-trip through pinned
# books (finding 1), book_ref must be validated/parse-safe (finding 2), and
# expiry accepts both ISO and YYYYMMDD (finding 4). ---


def test_pin_option_leg_persists_strike_expiry_right_and_round_trips(store):
    client = _client(store)
    r = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": "2026-09-18", "right": "C"}]},
    )
    assert r.status_code == 200
    pinned = r.json()
    pos = pinned["positions"][0]
    assert pos["sec_type"] == "OPT"
    assert pos["strike"] == 440.0
    # expiry normalized to YYYYMMDD (finding 4) regardless of ISO input.
    assert pos["expiry"] == "20260918"
    assert pos["right"] == "C"

    r2 = client.get(f"/api/book/{pinned['snapshot_id']}")
    assert r2.status_code == 200
    assert r2.json() == pinned


def test_pin_stock_leg_has_null_option_fields(store):
    client = _client(store)
    r = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 2}]})
    pos = r.json()["positions"][0]
    assert pos["strike"] is None
    assert pos["expiry"] is None
    assert pos["right"] is None


def test_two_books_differing_only_by_strike_get_different_snapshot_ids(store):
    client = _client(store)
    r1 = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": "20260918", "right": "C"}]},
    )
    r2 = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 2, "strike": 460.0, "expiry": "20260918", "right": "C"}]},
    )
    assert r1.json()["snapshot_id"] != r2.json()["snapshot_id"]


def test_book_ref_with_invalid_format_is_422_not_500(store):
    client = _client(store)
    r = client.get("/api/book/does-not-exist")
    # "does-not-exist" doesn't match the 12-hex-char snapshot id shape.
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_book_ref_path_traversal_is_422_not_500(store):
    # {"book_ref": "../instruments"} would resolve to {root}/instruments.json
    # (A2's instrument-metadata store, a dict keyed by symbol with no
    # "positions" key) if the ref weren't format-validated -> KeyError -> 500.
    store.write_instrument_metadata("SPY", {"exchange": "ARCA"})
    from quantmind.api.routers.book import read_book_positions
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        read_book_positions(store, "../instruments")
    assert exc_info.value.status_code == 422


def test_corrupted_book_snapshot_file_is_422_not_500(store):
    from quantmind.api.routers.book import _books_dir

    bad_id = "abcdef012345"
    (_books_dir(store) / f"{bad_id}.json").write_text("{not valid json")
    client = _client(store)
    r = client.get(f"/api/book/{bad_id}")
    assert r.status_code == 422
    assert "corrupted" in r.json()["detail"].lower()


def test_expiry_accepts_iso_and_yyyymmdd_forms(store):
    client = _client(store)
    r_iso = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 1, "strike": 440.0, "expiry": "2026-09-18", "right": "C"}]},
    )
    r_compact = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 1, "strike": 440.0, "expiry": "20260918", "right": "C"}]},
    )
    assert r_iso.status_code == r_compact.status_code == 200
    # Same normalized form -> same snapshot id.
    assert r_iso.json()["snapshot_id"] == r_compact.json()["snapshot_id"]
    assert r_iso.json()["positions"][0]["expiry"] == "20260918"


def test_expiry_rejects_unparseable_form(store):
    client = _client(store)
    r = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 1, "strike": 440.0, "expiry": "Sep-2026", "right": "C"}]},
    )
    assert r.status_code == 422


# --- Batch-2 final review, item 1: pin-time all-or-none. A partial option
# descriptor (any strict subset of strike/expiry/right) must be refused AT PIN
# TIME — before it can ever persist — and any pre-fix snapshot already on disk
# carrying a partial descriptor must be refused at read time regardless of its
# persisted sec_type ("re-pin with explicit legs"). ---


@pytest.mark.parametrize(
    "leg_fields, missing",
    [
        ({"strike": 450.0}, ["expiry", "right"]),
        ({"expiry": "20260918"}, ["strike", "right"]),
        ({"right": "C"}, ["strike", "expiry"]),  # right-only stays covered
        ({"strike": 450.0, "expiry": "20260918"}, ["right"]),
        ({"strike": 450.0, "right": "C"}, ["expiry"]),
        ({"expiry": "20260918", "right": "C"}, ["strike"]),
    ],
)
def test_pin_partial_option_descriptor_is_422_naming_missing_fields(store, leg_fields, missing):
    client = _client(store)
    r = client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 1, **leg_fields}]}
    )
    assert r.status_code == 422
    detail = str(r.json()["detail"])
    assert "SPY" in detail
    for field in missing:
        assert field in detail


def _write_legacy_partial_snapshot(store, snapshot_id="aaaaaaaaaaaa", sec_type="STK"):
    """A pre-fix snapshot as it exists on disk: sec_type STK with a partial
    option descriptor (strike+expiry, no right) — exactly what /api/book/pin
    used to persist before the pin-time all-or-none guard."""
    import json as _json

    from quantmind.api.routers.book import _books_dir

    payload = {
        "snapshot_id": snapshot_id,
        "valuation_ts": "2026-07-24T00:00:00Z",
        "base_currency": "USD",
        "positions": [
            {
                "con_id": 1,
                "symbol": "SPY",
                "qty": 2,
                "sec_type": sec_type,
                "multiplier": 1.0,
                "strike": 450.0,
                "expiry": "20260918",
                "right": None,
            }
        ],
    }
    (_books_dir(store) / f"{snapshot_id}.json").write_text(_json.dumps(payload))
    return snapshot_id


def test_legacy_partial_snapshot_is_422_from_read_book_positions(store):
    from fastapi import HTTPException

    from quantmind.api.routers.book import read_book_positions

    ref = _write_legacy_partial_snapshot(store)
    with pytest.raises(HTTPException) as exc_info:
        read_book_positions(store, ref)
    assert exc_info.value.status_code == 422
    assert "re-pin with explicit legs" in exc_info.value.detail


def test_legacy_partial_snapshot_is_422_from_every_consumer(store):
    # The persisted-partial refusal must hold at EVERY book_ref consumer, not
    # just whatif: hedge, macro, lab book-regression, portfolio, options
    # book-greeks all resolve refs through read_book_positions.
    client = _client(store)
    ref = _write_legacy_partial_snapshot(store)

    requests = [
        ("post", "/api/whatif", {"book_ref": ref, "years": 1}),
        (
            "post",
            "/api/hedge",
            {"book_ref": ref, "objective": {"kind": "beta_target", "value": 0.0}},
        ),
        ("get", f"/api/macro?book_ref={ref}", None),
        ("post", "/api/lab/book-regression", {"book_ref": ref}),
        ("get", f"/api/portfolio?book_ref={ref}", None),
        ("post", "/api/options/book-greeks", {"book_ref": ref}),
    ]
    for method, url, body in requests:
        r = client.get(url) if method == "get" else client.post(url, json=body)
        assert r.status_code == 422, f"{url} returned {r.status_code}"
        assert "re-pin with explicit legs" in str(r.json()["detail"]), url


def test_broker_sourced_option_leg_without_strike_is_422_on_book_ref_resolution(store):
    # A live broker Position carries no strike/expiry/right (portfolio.py's
    # Position type doesn't have those fields) — if it happens to be an OPT
    # leg, auto-pinning it must not silently let downstream consumers treat
    # it as a bare stock position (finding 1.iv: honest refusal).
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=2, sec_type="OPT", multiplier=100.0),),
        as_of="2026-07-24T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.get("/api/book/current")
    assert r.status_code == 200
    snapshot_id = r.json()["snapshot_id"]

    from quantmind.api.routers.book import read_book_positions
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        read_book_positions(store, snapshot_id)
    assert exc_info.value.status_code == 422
    assert "strike/expiry" in exc_info.value.detail
