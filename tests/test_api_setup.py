"""First-user setup/readiness API contract.

The endpoint is diagnostic only: it reflects app connection state, cached
market evidence, and persisted book snapshots without calling the broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.api.routers.book import _account_fingerprint
import quantmind.api.main as api_main
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore


class BrokerThatMustNotBeCalled:
    async def get_portfolio(self):
        raise AssertionError("setup status must not call the broker")


def _bars(end: datetime) -> pd.DataFrame:
    index = pd.bdate_range(end=end.date(), periods=5)
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=index)
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def _client(store: BarStore, broker=None) -> TestClient:
    app = create_app(store=store, benchmark="SPY", broker=broker)
    return TestClient(app, base_url="http://127.0.0.1")


def _seed_market(store: BarStore, end: datetime) -> None:
    store.write_symbol_map({"SPY": 1})
    store.write_instrument_metadata("SPY", {"currency": "USD", "exchange": "ARCA"})
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_bars(end),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=end.date().isoformat()),
    )
    for name in ["US10Y", "US2Y", "US3M", "NET_LIQUIDITY"]:
        store.write_series(
            name,
            pd.Series([1.0, 1.1], index=pd.date_range(end=end.date(), periods=2)),
        )


def test_setup_status_prioritizes_starting_gateway_for_an_empty_install(tmp_path):
    response = _client(BarStore(tmp_path)).get("/api/setup/status")

    assert response.status_code == 200
    assert response.json() == {
        "overall": "needs_attention",
        "api": {"status": "ready", "version": "0.4.0.0"},
        "broker": {
            "status": "unavailable",
            "provider": "IBKR",
            "mode": None,
            "error": None,
        },
            "market_data": {
                "status": "empty",
                "symbols": 1,
                "ready_symbols": 0,
                "missing_symbols": ["SPY"],
            "stale_symbols": [],
            "corrupt_symbols": [],
            "series": 0,
            "as_of": None,
            "age_days": None,
        },
        "macro_data": {
            "status": "empty",
            "required_series": 4,
            "ready_series": 0,
            "missing_series": ["NET_LIQUIDITY", "US10Y", "US2Y", "US3M"],
            "stale_series": [],
            "corrupt_series": [],
            "as_of": None,
            "age_days": None,
        },
        "options_data": {
            "status": "not_required",
            "total_positions": 0,
            "priced_positions": 0,
            "missing_contracts": [],
            "stale_chains": [],
            "chain_as_of": None,
            "chain_age_days": None,
        },
        "book": {
            "status": "not_pinned",
            "snapshot_count": 0,
            "latest_snapshot_id": None,
            "valuation_ts": None,
            "option_positions": 0,
            "age_days": None,
            "source": None,
            "account_fingerprint": None,
            "broker_mode": None,
            "unsupported_currencies": [],
            "unsupported_security_types": [],
            "reason": None,
        },
        "next_action": "start_gateway",
    }


def test_setup_status_reports_ready_after_market_sync_and_book_pin(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    client = _client(store, broker=BrokerThatMustNotBeCalled())
    pinned = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 10}]},
    )
    assert pinned.status_code == 200

    response = client.get("/api/setup/status")

    assert response.status_code == 200
    body = response.json()
    assert body["overall"] == "ready"
    assert body["broker"]["status"] == "connected"
    assert body["market_data"] == {
        "status": "ready",
        "symbols": 1,
        "ready_symbols": 1,
        "missing_symbols": [],
        "stale_symbols": [],
        "corrupt_symbols": [],
        "series": 4,
        "as_of": now.date().isoformat(),
        "age_days": 0,
    }
    assert body["macro_data"]["status"] == "ready"
    assert body["macro_data"]["ready_series"] == 4
    assert body["book"]["status"] == "ready"
    assert body["book"]["snapshot_count"] == 1
    assert body["book"]["latest_snapshot_id"] == pinned.json()["snapshot_id"]
    assert body["book"]["valuation_ts"] == pinned.json()["valuation_ts"]
    assert body["book"]["option_positions"] == 0
    assert body["book"]["age_days"] == 0
    assert body["book"]["source"] == "manual"
    assert body["book"]["reason"] is None
    assert body["next_action"] == "ready"


def test_setup_status_requests_a_sync_when_connected_cache_is_stale(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc) - timedelta(days=10))

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["market_data"]["status"] == "stale"
    assert body["market_data"]["age_days"] >= 7
    assert body["next_action"] == "sync_market_data"


def test_setup_status_ignores_a_corrupted_book_snapshot(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    (books / "badbadbadbad.json").write_text("not-json")
    (books / "feedfacecafe.json").write_text(
        json.dumps(
            {
                "snapshot_id": "feedfacecafe",
                "valuation_ts": "2026-09-04T00:00:00Z",
                "positions": ["not-a-position"],
            }
        )
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    assert response.json()["book"]["status"] == "not_pinned"
    assert response.json()["next_action"] == "pin_book"


def test_broker_mode_covers_gateway_and_tws_default_ports():
    assert api_main.broker_mode_for_port(4002) == "paper"
    assert api_main.broker_mode_for_port(7497) == "paper"
    assert api_main.broker_mode_for_port(4001) == "live"
    assert api_main.broker_mode_for_port(7496) == "live"
    assert api_main.broker_mode_for_port(5000) == "custom"


def test_setup_status_prioritizes_account_selection_for_a_multi_account_session(tmp_path):
    app = create_app(store=BarStore(tmp_path), benchmark="SPY")
    app.state.broker_connection_error = "account_selection_required"
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.get("/api/setup/status")

    assert response.status_code == 200
    assert response.json()["next_action"] == "configure_account"


def test_setup_status_counts_option_positions_in_the_latest_book(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    client = _client(store, broker=BrokerThatMustNotBeCalled())
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": -2,
                    "strike": 700,
                    "expiry": "20261218",
                    "right": "P",
                }
            ]
        },
    )
    assert pinned.status_code == 200

    response = client.get("/api/setup/status")

    assert response.status_code == 200
    assert response.json()["book"]["option_positions"] == 1


def test_setup_status_waits_while_the_broker_is_connecting(tmp_path):
    app = create_app(store=BarStore(tmp_path), benchmark="SPY")
    app.state.broker_connection_status = "connecting"
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.get("/api/setup/status")

    assert response.status_code == 200
    assert response.json()["broker"]["status"] == "connecting"
    assert response.json()["next_action"] == "wait_for_gateway"


def test_setup_status_selects_the_newest_valid_snapshot(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    for snapshot_id, valuation_ts, sec_type in [
        ("aaaaaaaaaaaa", "2026-09-03T12:00:00Z", "STK"),
        ("bbbbbbbbbbbb", "2026-09-04T12:00:00Z", "OPT"),
    ]:
        (books / f"{snapshot_id}.json").write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "valuation_ts": valuation_ts,
                    "base_currency": "USD",
                    "positions": [
                        {
                            "symbol": "SPY",
                            "qty": 1,
                            "con_id": 1,
                            "sec_type": sec_type,
                            "multiplier": 100 if sec_type == "OPT" else 1,
                            "strike": 700 if sec_type == "OPT" else None,
                            "expiry": "20261218" if sec_type == "OPT" else None,
                            "right": "C" if sec_type == "OPT" else None,
                        }
                    ],
                }
            )
        )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    book = response.json()["book"]
    assert book["snapshot_count"] == 2
    assert book["latest_snapshot_id"] == "bbbbbbbbbbbb"
    assert book["valuation_ts"] == "2026-09-04T12:00:00Z"
    assert book["option_positions"] == 1


def test_setup_status_ignores_non_object_snapshot_json(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    (books / "abcdef012345.json").write_text("null")

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    assert response.json()["book"]["status"] == "not_pinned"
    assert response.json()["next_action"] == "pin_book"


def test_setup_status_is_incomplete_when_any_mapped_symbol_has_no_bars(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_bars(now),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=now.date().isoformat()),
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    market = response.json()["market_data"]
    assert market["status"] == "incomplete"
    assert market["ready_symbols"] == 1
    assert market["missing_symbols"] == ["QQQ"]
    assert response.json()["next_action"] == "sync_market_data"


def test_setup_status_requires_configured_benchmark_even_when_unmapped(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write_symbol_map({"QQQ": 2})
    store.write_required_symbols(["QQQ"])
    store.write_bars(
        con_id=2,
        bar_size="1d",
        bars=_bars(now),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=now.date().isoformat()),
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    market = response.json()["market_data"]
    assert market["status"] == "incomplete"
    assert market["missing_symbols"] == ["SPY"]


def test_setup_status_ignores_orphaned_symbol_map_entries_after_sync_manifest(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write_symbol_map({"SPY": 1, "OLD_HOLDING": 99})
    store.write_required_symbols(["SPY"])
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_bars(now),
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=now.date().isoformat()),
    )
    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    market = response.json()["market_data"]
    assert market["status"] == "ready"
    assert market["symbols"] == 1


def test_setup_status_uses_the_oldest_required_market_watermark(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    for con_id, end in [(1, now), (2, old)]:
        store.write_bars(
            con_id=con_id,
            bar_size="1d",
            bars=_bars(end),
            meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=end.date().isoformat()),
        )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    market = response.json()["market_data"]
    assert market["status"] == "stale"
    assert market["as_of"] == old.date().isoformat()
    assert market["stale_symbols"] == ["QQQ"]
    assert market["ready_symbols"] == 1


def test_setup_status_requires_a_fresh_non_empty_snapshot(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    (books / "aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "snapshot_id": "aaaaaaaaaaaa",
                "valuation_ts": "2020-01-01T00:00:00Z",
                "base_currency": "USD",
                "source": "manual",
                "positions": [
                    {
                        "symbol": "SPY",
                        "qty": 1,
                        "con_id": 1,
                        "sec_type": "STK",
                        "multiplier": 1,
                        "currency": "USD",
                    }
                ],
            }
        )
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.json()["book"]["status"] == "stale"
    assert response.json()["book"]["reason"] == "stale_snapshot"
    assert response.json()["next_action"] == "pin_book"


def test_setup_status_rejects_a_snapshot_from_another_live_account(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (books / "aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "snapshot_id": "aaaaaaaaaaaa",
                "valuation_ts": valuation_ts,
                "base_currency": "USD",
                "source": "live_ibkr",
                "account_fingerprint": _account_fingerprint("U-OLD"),
                "broker_mode": "paper",
                "positions": [
                    {
                        "symbol": "SPY",
                        "qty": 1,
                        "con_id": 1,
                        "sec_type": "STK",
                        "multiplier": 1,
                        "currency": "USD",
                    }
                ],
            }
        )
    )
    app = create_app(store=store, benchmark="SPY", broker=BrokerThatMustNotBeCalled())
    app.state.broker_account_id = "U-NEW"
    app.state.broker_mode = "paper"

    response = TestClient(app, base_url="http://127.0.0.1").get("/api/setup/status")

    assert response.json()["book"]["status"] == "stale"
    assert response.json()["book"]["reason"] == "account_mismatch"


def test_setup_status_blocks_unsupported_security_types(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (books / "aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "snapshot_id": "aaaaaaaaaaaa",
                "valuation_ts": valuation_ts,
                "base_currency": "USD",
                "source": "manual",
                "positions": [
                    {
                        "symbol": "ES",
                        "qty": 1,
                        "con_id": 99,
                        "sec_type": "FUT",
                        "multiplier": 50,
                        "currency": "USD",
                    }
                ],
            }
        )
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    book = response.json()["book"]
    assert book["status"] == "unsupported"
    assert book["reason"] == "unsupported_security_type"
    assert book["unsupported_security_types"] == ["FUT"]
    assert response.json()["next_action"] == "resolve_instruments"


def test_setup_status_requires_each_macro_series_before_ready(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    (tmp_path / "series" / "US2Y.parquet").unlink()

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    macro = response.json()["macro_data"]
    assert macro["status"] == "incomplete"
    assert macro["missing_series"] == ["US2Y"]
    assert macro["ready_series"] == 3
    assert response.json()["next_action"] == "sync_market_data"


def test_setup_status_requires_the_exact_held_option_contract(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    client = _client(store, broker=BrokerThatMustNotBeCalled())
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": -2,
                    "strike": 700,
                    "expiry": "20261218",
                    "right": "C",
                }
            ]
        },
    )
    assert pinned.status_code == 200

    missing = client.get("/api/setup/status").json()
    assert missing["options_data"]["status"] == "missing"
    assert missing["options_data"]["missing_contracts"] == ["SPY 20261218 700 C"]
    assert missing["next_action"] == "sync_option_data"

    OptionsStore(tmp_path).write_chain(
        "SPY",
        pd.DataFrame(
            [
                {
                    "expiry": "20261218",
                    "strike": 700.0,
                    "right": "C",
                    "con_id": 999,
                    "bid": 2.0,
                    "ask": 2.2,
                    "iv": 0.35,
                    "delta": 0.4,
                    "multiplier": 100.0,
                }
            ]
        ),
        OptionsSnapshotMeta(as_of=now.date().isoformat(), spot=104.0),
    )

    ready = client.get("/api/setup/status").json()
    assert ready["options_data"]["status"] == "ready"
    assert ready["options_data"]["priced_positions"] == 1
    assert ready["overall"] == "ready"


def test_setup_blocks_a_non_usd_book_instead_of_summing_local_prices(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    books = tmp_path / "books"
    books.mkdir()
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (books / "aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "snapshot_id": "aaaaaaaaaaaa",
                "valuation_ts": valuation_ts,
                "base_currency": "USD",
                "source": "manual",
                "positions": [
                    {
                        "symbol": "ASML",
                        "qty": 10,
                        "con_id": 1,
                        "sec_type": "STK",
                        "multiplier": 1,
                        "currency": "EUR",
                        "exchange": "AEB",
                    }
                ],
            }
        )
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.json()["book"]["status"] == "unsupported"
    assert response.json()["book"]["unsupported_currencies"] == ["EUR"]
    assert response.json()["next_action"] == "resolve_currency"
