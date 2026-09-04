"""API contract tests for the book-flow spine (wave-3 Task A1):
POST /api/book/pin, GET /api/book/{id}, GET /api/book/current. A BookSnapshot
pins either the live broker's book or a posted position list as immutable
JSON under `{store.root}/books/{snapshot_id}.json` — never through
datastore/store.py (A2 owns that file this wave).

Serialization policy: UTC ISO Z timestamps, unknown book_ref/symbols ->
structured 422, never a 500 (repo-wide policy, pattern: routers/whatif.py).
"""

from __future__ import annotations

import json

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
    s.write_instrument_metadata("SPY", {"currency": "USD", "exchange": "ARCA"})
    s.write_instrument_metadata("QQQ", {"currency": "USD", "exchange": "NASDAQ"})
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


def test_manual_pin_rejects_currency_that_conflicts_with_instrument_master(store):
    response = _client(store).post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 1, "currency": "EUR"}]},
    )

    assert response.status_code == 422
    assert "currency" in response.json()["detail"]


def test_manual_pin_without_authoritative_currency_cannot_enter_analysis(tmp_path):
    store = BarStore(tmp_path)
    store.write_symbol_map({"MYSTERY": 9})
    client = _client(store)

    pinned = client.post(
        "/api/book/pin", json={"positions": [{"symbol": "MYSTERY", "qty": 1}]}
    )

    assert pinned.status_code == 200
    assert pinned.json()["positions"][0]["currency"] == "UNKNOWN"
    from quantmind.api.routers.book import read_book_positions
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="FX normalization"):
        read_book_positions(store, pinned.json()["snapshot_id"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", float("nan")),
        ("qty", float("inf")),
        ("qty", float("-inf")),
        ("strike", float("nan")),
        ("multiplier", float("inf")),
    ],
)
def test_pin_rejects_nonfinite_position_terms_before_persisting(store, field, value):
    # An API boundary must never mint a persisted book whose quantity or
    # option terms poison downstream calculations and serialize as null.
    client = _client(store)
    position = {"symbol": "SPY", "qty": 1, field: value}

    response = client.post(
        "/api/book/pin",
        content=json.dumps({"positions": [position]}, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "finite" in response.json()["detail"][0]["msg"]
    assert not (store.root / "books").exists()


def test_pin_no_broker_no_positions_is_503_and_does_not_persist(store):
    client = _client(store, broker=None)
    r = client.post("/api/book/pin", json={})
    assert r.status_code == 503
    assert "broker" in r.json()["detail"].lower()
    assert not (store.root / "books").exists()


def test_pin_explicit_empty_positions_is_a_confirmed_empty_book(store):
    client = _client(store, broker=None)
    r = client.post("/api/book/pin", json={"positions": []})
    assert r.status_code == 200
    assert r.json()["positions"] == []


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


def test_current_book_with_no_broker_is_503_and_does_not_persist(store):
    client = _client(store, broker=None)
    r = client.get("/api/book/current")
    assert r.status_code == 503
    assert "broker" in r.json()["detail"].lower()
    assert not (store.root / "books").exists()


def test_current_book_with_broker_is_a_read_only_preview(store):
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
    assert "snapshot_id" not in body
    assert not (store.root / "books").exists()


def test_pin_current_book_with_broker_persists_a_resolvable_snapshot(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=7, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    pinned = client.post("/api/book/pin", json={})
    assert pinned.status_code == 200

    r = client.get(f"/api/book/{pinned.json()['snapshot_id']}")
    assert r.status_code == 200
    assert r.json() == pinned.json()


def test_broker_instrument_identity_survives_preview_pin_and_read(store):
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=987654,
                symbol="ASML",
                qty=12,
                sec_type="STK",
                multiplier=1.0,
                currency="EUR",
                exchange="AEB",
            ),
        ),
        as_of="2026-09-04T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))

    preview = client.get("/api/book/current")
    pinned = client.post("/api/book/pin", json={})
    loaded = client.get(f"/api/book/{pinned.json()['snapshot_id']}")

    assert preview.status_code == pinned.status_code == loaded.status_code == 200
    for body in (preview.json(), pinned.json(), loaded.json()):
        assert body["positions"][0]["currency"] == "EUR"
        assert body["positions"][0]["exchange"] == "AEB"

    persisted = json.loads(
        (store.root / "books" / f"{pinned.json()['snapshot_id']}.json").read_text()
    )
    assert persisted["positions"][0]["currency"] == "EUR"
    assert persisted["positions"][0]["exchange"] == "AEB"


def test_legacy_book_without_instrument_identity_remains_readable(store):
    snapshot_id = "abcdef012345"
    books = store.root / "books"
    books.mkdir()
    (books / f"{snapshot_id}.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "valuation_ts": "2026-09-04T12:00:00Z",
                "base_currency": "USD",
                "positions": [
                    {
                        "symbol": "SPY",
                        "qty": 10,
                        "con_id": 1,
                        "sec_type": "STK",
                        "multiplier": 1,
                    }
                ],
            }
        )
    )

    response = _client(store).get(f"/api/book/{snapshot_id}")

    assert response.status_code == 200
    position = response.json()["positions"][0]
    assert position["currency"] is None
    assert position["exchange"] is None


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


def test_live_books_from_different_account_scopes_get_different_ids(store):
    from quantmind.api.routers.book import _pin_and_respond

    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="SPY",
                qty=10,
                currency="USD",
                exchange="ARCA",
            ),
        ),
        as_of="2026-09-04T12:00:00Z",
    )

    first = _pin_and_respond(
        store,
        portfolio,
        "2026-09-04T12:00:00Z",
        source="live_ibkr",
        account_fingerprint="aaaaaaaaaaaa",
        broker_mode="paper",
    )
    second = _pin_and_respond(
        store,
        portfolio,
        "2026-09-04T12:00:00Z",
        source="live_ibkr",
        account_fingerprint="bbbbbbbbbbbb",
        broker_mode="paper",
    )

    assert first.snapshot_id != second.snapshot_id


def test_v2_snapshot_rejects_valid_json_content_tampering(store):
    client = _client(store)
    pinned = client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]}
    ).json()
    path = store.root / "books" / f"{pinned['snapshot_id']}.json"
    payload = json.loads(path.read_text())
    payload["positions"][0]["qty"] = 999
    path.write_text(json.dumps(payload))

    response = client.get(f"/api/book/{pinned['snapshot_id']}")

    assert response.status_code == 422
    assert "corrupted" in response.json()["detail"]


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


def test_non_object_book_snapshot_file_is_422_not_500(store):
    from quantmind.api.routers.book import _books_dir

    bad_id = "abcdef012345"
    (_books_dir(store) / f"{bad_id}.json").write_text("null")

    response = _client(store).get(f"/api/book/{bad_id}")

    assert response.status_code == 422
    assert "corrupted" in response.json()["detail"].lower()


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


def test_broker_sourced_option_leg_without_strike_is_422_on_book_ref_resolution(store):
    # A live broker Position carries no strike/expiry/right (portfolio.py's
    # Position type doesn't have those fields) — if it happens to be an OPT
    # leg, pinning it must not silently let downstream consumers treat
    # it as a bare stock position (finding 1.iv: honest refusal).
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="SPY",
                qty=2,
                sec_type="OPT",
                multiplier=100.0,
                currency="USD",
            ),
        ),
        as_of="2026-07-24T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.post("/api/book/pin", json={})
    assert r.status_code == 200
    snapshot_id = r.json()["snapshot_id"]

    from quantmind.api.routers.book import read_book_positions
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        read_book_positions(store, snapshot_id)
    assert exc_info.value.status_code == 422
    assert "strike/expiry/right" in exc_info.value.detail


def test_persisted_option_leg_without_right_is_refused(store):
    from quantmind.api.routers.book import _books_dir, read_book_positions
    from fastapi import HTTPException

    snapshot_id = "abcdef012345"
    (_books_dir(store) / f"{snapshot_id}.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "valuation_ts": "2026-09-04T12:00:00Z",
                "base_currency": "USD",
                "positions": [
                    {
                        "symbol": "SPY",
                        "qty": 1,
                        "con_id": 1,
                        "sec_type": "OPT",
                        "multiplier": 100,
                        "strike": 700,
                        "expiry": "20261218",
                        "right": None,
                        "currency": "USD",
                    }
                ],
            }
        )
    )

    with pytest.raises(HTTPException, match="strike/expiry/right"):
        read_book_positions(store, snapshot_id)


@pytest.mark.parametrize("sec_type", ["FUT", "CFD", "BOND"])
def test_resolved_non_stock_contract_type_is_preserved_and_refused(store, sec_type):
    from fastapi import HTTPException

    from quantmind.api.routers._shared import refuse_unsupported_contract_legs
    from quantmind.api.routers.book import _books_dir, read_book_positions

    snapshot_id = "abcdef012345"
    (_books_dir(store) / f"{snapshot_id}.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "valuation_ts": "2026-09-04T12:00:00Z",
                "base_currency": "USD",
                "positions": [
                    {
                        "symbol": "SPY",
                        "qty": 1,
                        "con_id": 1,
                        "sec_type": sec_type,
                        "multiplier": 1,
                        "currency": "USD",
                    }
                ],
            }
        )
    )

    positions = read_book_positions(store, snapshot_id)

    assert positions[0].sec_type == sec_type
    with pytest.raises(HTTPException, match="cannot value"):
        refuse_unsupported_contract_legs(positions, route_name="What-If")


def test_broker_sourced_option_terms_survive_pin_and_book_ref_resolution(store):
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=777,
                symbol="SPY",
                qty=-2,
                sec_type="OPT",
                multiplier=100.0,
                strike=700.0,
                expiry="20261218",
                right="P",
                currency="USD",
            ),
        ),
        as_of="2026-09-04T00:00:00Z",
    )
    client = _client(store, broker=FakeBroker(portfolio))

    pinned = client.post("/api/book/pin", json={})

    assert pinned.status_code == 200
    assert pinned.json()["positions"][0]["strike"] == 700.0
    assert pinned.json()["positions"][0]["expiry"] == "20261218"
    assert pinned.json()["positions"][0]["right"] == "P"

    from quantmind.api.routers.book import read_book_positions

    legs = read_book_positions(store, pinned.json()["snapshot_id"])
    assert legs[0].con_id == 777
    assert legs[0].strike == 700.0
    assert legs[0].expiry == "20261218"
    assert legs[0].right == "P"
