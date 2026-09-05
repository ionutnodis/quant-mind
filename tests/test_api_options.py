"""API contract tests for the options domain (Task A3):
GET /api/options/{underlier}/chain (cached, staleness-stamped, never-500) and
POST /api/options/book-greeks (thin composition over exposure/book_greeks.py
+ risk/options.py, IV/spot sourced from the store — never a live IB call).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.risk.options import bs_greeks


def _bars(n=10, price=452.0, end=None):
    idx = pd.bdate_range(end=end or date.today(), periods=n)
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
    s.write_instrument_metadata(
        "SPY", {"con_id": 1, "currency": "USD", "exchange": "ARCA"}
    )
    s.write_instrument_metadata(
        "QQQ", {"con_id": 2, "currency": "USD", "exchange": "NASDAQ"}
    )
    return s


@pytest.fixture
def client(store):
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _option_expiry(days_out: int = 45) -> str:
    return (date.today() + timedelta(days=days_out)).strftime("%Y%m%d")


def _chain_df():
    expiry = _option_expiry()
    observed_at = f"{date.today().isoformat()}T15:30:00Z"
    return pd.DataFrame(
        {
            "expiry": [expiry, expiry, expiry, expiry],
            "strike": [440.0, 440.0, 460.0, 460.0],
            "right": ["C", "P", "C", "P"],
            "con_id": [1001, 1002, 1003, 1004],
            "bid": [10.1, 8.2, 3.5, 14.3],
            "ask": [10.3, 8.4, 3.7, 14.5],
            "iv": [0.18, 0.20, 0.19, 0.21],
            "delta": [0.55, -0.45, 0.40, -0.60],
            "multiplier": [100.0, 100.0, 100.0, 100.0],
            "observed_at": [observed_at] * 4,
            "market_data_type": [1] * 4,
        }
    )


def _snapshot_timestamp(as_of: str) -> str:
    return as_of if "T" in as_of else f"{as_of}T15:30:00Z"


def _with_quote_evidence(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    return frame.assign(
        observed_at=_snapshot_timestamp(as_of),
        market_data_type=1,
    )


def _write_spy_chain(store, as_of="2026-07-24"):
    snapshot_timestamp = _snapshot_timestamp(as_of)
    OptionsStore(store.root).write_chain(
        "SPY",
        _with_quote_evidence(_chain_df(), snapshot_timestamp),
        OptionsSnapshotMeta(
            as_of=snapshot_timestamp,
            spot=452.0,
            underlier_con_id=1,
        ),
    )


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
    assert {quote["con_id"] for quote in body["quotes"]} == {
        1001,
        1002,
        1003,
        1004,
    }
    assert {quote["observed_at"] for quote in body["quotes"]} == {
        f"{date.today().isoformat()}T15:30:00Z"
    }
    assert {quote["market_data_type"] for quote in body["quotes"]} == {1}
    assert body["stale"] is False
    # one expiry, two strikes -> smile has 1 expiry group with 2 strike points
    assert len(body["smile"]) == 1
    assert body["smile"][0]["expiry"] == _option_expiry()
    strikes = sorted(p["strike"] for p in body["smile"][0]["points"])
    assert strikes == [440.0, 460.0]
    # smile IV at 440 averages call (0.18) and put (0.20)
    point_440 = next(p for p in body["smile"][0]["points"] if p["strike"] == 440.0)
    assert point_440["iv"] == pytest.approx((0.18 + 0.20) / 2)


def test_chain_smile_fails_closed_for_ambiguous_same_terms(client, store):
    ambiguous = pd.concat(
        [
            _chain_df(),
            _chain_df().iloc[[0]].assign(con_id=9001, iv=0.75),
        ],
        ignore_index=True,
    )
    OptionsStore(store.root).write_chain(
        "SPY",
        ambiguous,
        OptionsSnapshotMeta(
            as_of=_snapshot_timestamp(str(date.today())),
            spot=452.0,
            underlier_con_id=1,
        ),
    )

    response = client.get("/api/options/SPY/chain")

    assert response.status_code == 200
    body = response.json()
    assert {quote["con_id"] for quote in body["quotes"]} >= {1001, 9001}
    point_440 = next(
        point
        for point in body["smile"][0]["points"]
        if point["strike"] == 440.0
    )
    assert point_440["iv"] is None


def test_chain_is_unavailable_after_underlier_contract_remap(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    store.write_symbol_map({"SPY": 99, "QQQ": 2})
    store.write_instrument_metadata(
        "SPY", {"con_id": 99, "currency": "USD", "exchange": "ARCA"}
    )

    response = client.get("/api/options/SPY/chain")

    assert response.status_code == 200
    assert response.json()["missing"] is True


def test_chain_with_a_corrupt_symbol_map_is_structured_unavailable(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    (store.root / "symbols.json").write_text("not json")

    response = client.get("/api/options/SPY/chain")

    assert response.status_code == 200
    assert response.json()["missing"] is True


def test_book_greeks_rejects_chain_from_a_different_underlier_contract(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    store.write_symbol_map({"SPY": 99, "QQQ": 2})
    store.write_instrument_metadata(
        "SPY", {"con_id": 99, "currency": "USD", "exchange": "ARCA"}
    )
    store.write_bars(
        con_id=99,
        bar_size="1d",
        bars=_bars(price=500.0),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())),
    )

    response = client.post(
        "/api/options/book-greeks",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 440.0,
                    "expiry": _option_expiry(),
                    "right": "C",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "underlier identity" in response.json()["detail"]


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


def test_book_greeks_rejects_live_book_from_a_different_account(client, store):
    from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond
    from quantmind.portfolio import Portfolio, Position

    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, currency="USD"),
        ),
        as_of="2026-09-04T12:00:00Z",
    )
    pinned = _pin_and_respond(
        store,
        portfolio,
        portfolio.as_of,
        source="live_ibkr",
        account_fingerprint=_account_fingerprint("DU_ACCOUNT_A"),
        broker_mode="paper",
    )
    client.app.state.broker_account_id = "DU_ACCOUNT_B"
    client.app.state.broker_mode = "paper"

    response = client.post(
        "/api/options/book-greeks", json={"book_ref": pinned.snapshot_id}
    )

    assert response.status_code == 409
    assert "account" in response.json()["detail"]


def test_book_greeks_rejects_a_pinned_non_equity_contract(client, store):
    from quantmind.api.routers.book import _pin_and_respond
    from quantmind.portfolio import Portfolio, Position

    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="SPY",
                qty=1,
                sec_type="FUT",
                multiplier=50,
                currency="USD",
            ),
        ),
        as_of="2026-09-04T12:00:00Z",
    )
    pinned = _pin_and_respond(
        store,
        portfolio,
        portfolio.as_of,
        source="manual",
    )

    response = client.post(
        "/api/options/book-greeks", json={"book_ref": pinned.snapshot_id}
    )

    assert response.status_code == 422
    assert "FUT" in response.json()["detail"]


def test_book_greeks_option_leg_pulls_iv_from_cached_chain(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    expiry = _option_expiry()
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": expiry, "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 200
    row = r.json()["underlyings"][0]

    expiry_years = (
        datetime.strptime(expiry, "%Y%m%d").date() - date.today()
    ).days / 365.25
    expected = bs_greeks(452.0, 440.0, expiry_years, 0.0, 0.18, True)
    assert row["delta"] == pytest.approx(2 * 100 * expected.delta, rel=1e-4)
    assert row["gamma"] == pytest.approx(2 * 100 * expected.gamma, rel=1e-4)


def test_book_greeks_rejects_a_stale_option_chain(client, store):
    stale_chain_date = str(date.today() - timedelta(days=10))
    _write_spy_chain(store, as_of=stale_chain_date)

    response = client.post(
        "/api/options/book-greeks",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 440.0,
                    "expiry": _option_expiry(),
                    "right": "C",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "cached option chain" in response.json()["detail"]
    assert "stale" in response.json()["detail"]


def test_book_greeks_rejects_chain_when_its_weakest_quote_is_stale(client, store):
    stale_date = date.today() - timedelta(days=10)
    stale_observed_at = f"{stale_date.isoformat()}T20:00:00Z"
    frame = _chain_df()
    frame.loc[0, "observed_at"] = stale_observed_at
    frame.loc[0, "market_data_type"] = 4
    OptionsStore(store.root).write_chain(
        "SPY",
        frame,
        OptionsSnapshotMeta(
            as_of=stale_observed_at,
            spot=452.0,
            underlier_con_id=1,
        ),
    )

    response = client.post(
        "/api/options/book-greeks",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 440.0,
                    "expiry": _option_expiry(),
                    "right": "C",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "cached option chain" in response.json()["detail"]
    assert "stale" in response.json()["detail"]


def test_book_greeks_rejects_a_stale_underlier_spot(client, store):
    stale_spot_date = date.today() - timedelta(days=10)
    store.write_symbol_map({"SPY": 1, "QQQ": 2, "OLD": 3})
    store.write_instrument_metadata(
        "OLD", {"con_id": 3, "currency": "USD", "exchange": "NYSE"}
    )
    store.write_bars(
        con_id=3,
        bar_size="1d",
        bars=_bars(price=100.0, end=stale_spot_date),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(stale_spot_date)),
    )

    response = client.post(
        "/api/options/book-greeks",
        json={"positions": [{"symbol": "OLD", "qty": 1}]},
    )

    assert response.status_code == 422
    assert "cached bars" in response.json()["detail"]
    assert "stale" in response.json()["detail"]


def test_book_greeks_as_of_is_weakest_spot_or_chain_observation(client, store):
    chain_date = (pd.Timestamp(date.today()) - pd.offsets.BDay(2)).date()
    chain_as_of = f"{chain_date.isoformat()}T15:30:00Z"
    newer_spot_date = (pd.Timestamp(date.today()) - pd.offsets.BDay(1)).date()
    _write_spy_chain(store, as_of=chain_as_of)
    store.write_symbol_map({"SPY": 1, "QQQ": 4})
    store.write_instrument_metadata(
        "QQQ", {"con_id": 4, "currency": "USD", "exchange": "NASDAQ"}
    )
    store.write_bars(
        con_id=4,
        bar_size="1d",
        bars=_bars(price=380.0, end=newer_spot_date),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(newer_spot_date)),
    )

    response = client.post(
        "/api/options/book-greeks",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 440.0,
                    "expiry": _option_expiry(),
                    "right": "C",
                },
                {"symbol": "QQQ", "qty": 1},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["as_of"] == chain_as_of


def test_book_greeks_inline_leg_rejects_ambiguous_same_terms(client, store):
    expiry = (date.today() + timedelta(days=45)).strftime("%Y%m%d")
    chain = pd.DataFrame(
        {
            "expiry": [expiry, expiry],
            "strike": [452.0, 452.0],
            "right": ["C", "C"],
            "con_id": [7001, 7002],
            "bid": [4.0, 8.0],
            "ask": [4.2, 8.2],
            "iv": [0.12, 0.42],
            "delta": [0.5, 0.5],
            "multiplier": [100.0, 100.0],
        }
    )
    OptionsStore(store.root).write_chain(
        "SPY",
        _with_quote_evidence(chain, str(date.today())),
        OptionsSnapshotMeta(
            as_of=_snapshot_timestamp(str(date.today())),
            spot=452.0,
            underlier_con_id=1,
        ),
    )

    response = client.post(
        "/api/options/book-greeks",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 452.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "no cached quote" in response.json()["detail"]


def test_book_greeks_unknown_option_leg_is_422_not_500(client, store):
    _write_spy_chain(store, as_of=str(date.today()))
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 1, "strike": 999.0, "expiry": _option_expiry(), "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 422


def test_book_greeks_option_leg_without_cached_chain_is_422(client):
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 1, "strike": 440.0, "expiry": _option_expiry(), "right": "C"},
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


def test_book_greeks_refuses_cross_currency_aggregation_until_legwise_fx_exists(store):
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", base_currency="GBP")
    c = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = c.post(
        "/api/options/book-greeks",
        json={"positions": [{"symbol": "SPY", "qty": 10}]},
    )

    assert response.status_code == 422
    assert "cross-currency" in response.json()["detail"]


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
    positions = [{"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": _option_expiry(), "right": "C"}]

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


def test_book_greeks_uses_persisted_live_contract_id_for_same_terms(client, store):
    from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond
    from quantmind.portfolio import Portfolio, Position

    expiry = (date.today() + timedelta(days=45)).strftime("%Y%m%d")
    chain = pd.DataFrame(
        {
            "expiry": [expiry, expiry],
            "strike": [452.0, 452.0],
            "right": ["C", "C"],
            "con_id": [7001, 7002],
            "bid": [4.0, 4.0],
            "ask": [4.2, 4.2],
            "iv": [0.12, 0.42],
            "delta": [0.5, 0.5],
            "multiplier": [100.0, 100.0],
        }
    )
    OptionsStore(store.root).write_chain(
        "SPY",
        _with_quote_evidence(chain, str(date.today())),
        OptionsSnapshotMeta(
            as_of=_snapshot_timestamp(str(date.today())),
            spot=452.0,
            underlier_con_id=1,
        ),
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=7002,
                symbol="SPY",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=452.0,
                expiry=expiry,
                right="C",
                currency="USD",
                exchange="SMART",
            ),
        ),
        as_of="2026-09-04T12:00:00Z",
    )
    account_id = "DU_ACCOUNT_A"
    pinned = _pin_and_respond(
        store,
        portfolio,
        "2026-09-04T12:00:00Z",
        source="live_ibkr",
        account_fingerprint=_account_fingerprint(account_id),
        broker_mode="paper",
    )
    client.app.state.broker_account_id = account_id
    client.app.state.broker_mode = "paper"

    response = client.post(
        "/api/options/book-greeks", json={"book_ref": pinned.snapshot_id}
    )

    assert response.status_code == 200
    years = (date.today() + timedelta(days=45) - date.today()).days / 365.25
    expected = bs_greeks(452.0, 452.0, years, 0.0, 0.42, True)
    assert response.json()["underlyings"][0]["gamma"] == pytest.approx(
        100 * expected.gamma, rel=1e-4
    )


def test_book_greeks_rejects_persisted_live_contract_without_exact_chain_id(client, store):
    from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond
    from quantmind.portfolio import Portfolio, Position

    expiry = (date.today() + timedelta(days=45)).strftime("%Y%m%d")
    chain = pd.DataFrame(
        {
            "expiry": [expiry],
            "strike": [452.0],
            "right": ["C"],
            "con_id": [7001],
            "bid": [4.0],
            "ask": [4.2],
            "iv": [0.25],
            "delta": [0.5],
            "multiplier": [100.0],
        }
    )
    OptionsStore(store.root).write_chain(
        "SPY",
        _with_quote_evidence(chain, str(date.today())),
        OptionsSnapshotMeta(
            as_of=_snapshot_timestamp(str(date.today())),
            spot=452.0,
            underlier_con_id=1,
        ),
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=7999,
                symbol="SPY",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=452.0,
                expiry=expiry,
                right="C",
                currency="USD",
                exchange="SMART",
            ),
        ),
        as_of="2026-09-04T12:00:00Z",
    )
    account_id = "DU_ACCOUNT_A"
    pinned = _pin_and_respond(
        store,
        portfolio,
        "2026-09-04T12:00:00Z",
        source="live_ibkr",
        account_fingerprint=_account_fingerprint(account_id),
        broker_mode="paper",
    )
    client.app.state.broker_account_id = account_id
    client.app.state.broker_mode = "paper"

    response = client.post(
        "/api/options/book-greeks", json={"book_ref": pinned.snapshot_id}
    )

    assert response.status_code == 422
    assert "no cached quote" in response.json()["detail"]


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
    iso_expiry = (date.today() + timedelta(days=45)).isoformat()
    payload = {
        "positions": [
            {"symbol": "SPY", "qty": 2, "strike": 440.0, "expiry": iso_expiry, "right": "C"},
        ]
    }
    r = client.post("/api/options/book-greeks", json=payload)
    assert r.status_code == 200
