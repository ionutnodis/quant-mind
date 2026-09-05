"""First-user setup/readiness API contract.

The endpoint is diagnostic only: it reflects app connection state, cached
market evidence, and persisted book snapshots without calling the broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond
import quantmind.api.main as api_main
from quantmind.datastore.store import (
    BarMeta,
    BarStore,
    PORTFOLIO_DISCOVERY_FAILURE_SYMBOL,
)
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.fx import EcbFxProvider, sync_ecb_fx
from quantmind.instruments.metadata import (
    DistributionPolicy,
    MetadataProvenanceV1,
    UcitsEtfProfileV1,
)
from quantmind.portfolio import Portfolio, Position


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


def _client(
    store: BarStore, broker=None, *, base_currency: str = "USD"
) -> TestClient:
    app = create_app(
        store=store,
        benchmark="SPY",
        broker=broker,
        base_currency=base_currency,
    )
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


def _write_valid_book(
    store: BarStore,
    *,
    valuation_ts: str,
    positions: list[dict],
    source: str = "manual",
    account_fingerprint: str | None = None,
    broker_mode: str | None = None,
    base_currency: str = "USD",
) -> str:
    """Persist the same integrity-checked v2 snapshots production writes."""
    portfolio = Portfolio(
        positions=tuple(
            Position(
                con_id=int(item["con_id"]),
                symbol=item["symbol"],
                qty=item["qty"],
                sec_type=item["sec_type"],
                multiplier=item["multiplier"],
                strike=item.get("strike"),
                expiry=item.get("expiry"),
                right=item.get("right"),
                currency=item.get("currency"),
                exchange=item.get("exchange"),
            )
            for item in positions
        ),
        as_of=valuation_ts,
    )
    snapshot = _pin_and_respond(
        store,
        portfolio,
        valuation_ts,
        source=source,
        account_fingerprint=account_fingerprint,
        broker_mode=broker_mode,
        base_currency=base_currency,
    )
    return snapshot.snapshot_id


def test_setup_status_prioritizes_starting_gateway_for_an_empty_install(tmp_path):
    response = _client(BarStore(tmp_path)).get("/api/setup/status")

    assert response.status_code == 200
    assert response.json() == {
        "overall": "needs_attention",
        "api": {"status": "ready", "version": "0.5.0.0"},
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
            "portfolio_discovery_error": None,
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
        "fx_data": {
            "status": "not_required",
            "base_currency": "USD",
            "required_currencies": [],
            "missing_currencies": [],
            "provider": None,
            "as_of": None,
        },
        "ucits_data": {
            "status": "not_required",
            "total_etfs": 0,
            "ready_profiles": 0,
            "missing_symbols": [],
            "stale_symbols": [],
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
    market_as_of = _bars(now).index[-1].date()
    market_age_days = int(
        np.busday_count(market_as_of.isoformat(), now.date().isoformat())
    )
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
        "as_of": market_as_of.isoformat(),
        "age_days": market_age_days,
        "portfolio_discovery_error": None,
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


def test_setup_exposes_a_typed_portfolio_discovery_failure_without_a_fake_symbol(
    tmp_path,
):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    store.write_required_symbols(["SPY", PORTFOLIO_DISCOVERY_FAILURE_SYMBOL])
    store.write_instrument_metadata(
        "SPY", {"con_id": 1, "currency": "USD", "provider": "ibkr"}
    )

    body = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()

    assert body["market_data"]["status"] == "incomplete"
    assert body["market_data"]["symbols"] == 1
    assert body["market_data"]["missing_symbols"] == []
    assert (
        body["market_data"]["portfolio_discovery_error"]
        == "live_portfolio_unavailable"
    )
    assert PORTFOLIO_DISCOVERY_FAILURE_SYMBOL not in str(body)
    assert body["next_action"] == "sync_market_data"


def test_setup_requires_repin_after_configured_base_currency_changes(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    assert _client(store).post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]}
    ).status_code == 200

    body = _client(
        store, broker=BrokerThatMustNotBeCalled(), base_currency="GBP"
    ).get("/api/setup/status").json()

    assert body["book"]["status"] == "stale"
    assert body["book"]["reason"] == "base_currency_mismatch"
    assert body["fx_data"]["base_currency"] == "GBP"
    assert body["next_action"] == "pin_book"


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


def test_setup_reports_corrupt_instrument_metadata_without_500(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    (tmp_path / "instruments.json").write_text("not json")

    response = _client(store).get("/api/setup/status")

    assert response.status_code == 200
    body = response.json()
    assert body["market_data"]["status"] == "ready"
    assert body["ucits_data"] == {
        "status": "incomplete",
        "total_etfs": 0,
        "ready_profiles": 0,
        "missing_symbols": ["INSTRUMENT_METADATA"],
        "stale_symbols": [],
    }


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
    older_id = _write_valid_book(
        store,
        valuation_ts="2026-09-03T12:00:00Z",
        positions=[
            {
                "symbol": "SPY",
                "qty": 1,
                "con_id": 1,
                "sec_type": "STK",
                "multiplier": 1,
            }
        ],
    )
    newest_id = _write_valid_book(
        store,
        valuation_ts="2026-09-04T12:00:00Z",
        positions=[
            {
                "symbol": "SPY",
                "qty": 1,
                "con_id": 1,
                "sec_type": "OPT",
                "multiplier": 100,
                "strike": 700,
                "expiry": "20261218",
                "right": "C",
            }
        ],
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    assert response.status_code == 200
    book = response.json()["book"]
    assert book["snapshot_count"] == 2
    assert older_id != newest_id
    assert book["latest_snapshot_id"] == newest_id
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
    store.write_instrument_metadata(
        "QQQ", {"con_id": 2, "currency": "USD", "provider": "ibkr"}
    )
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


def test_setup_required_universe_needs_matching_currency_metadata(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write_symbol_map({"SPY": 1})
    store.write_required_symbols(["SPY"])
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_bars(now),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST", adjusted_asof=now.date().isoformat()
        ),
    )

    missing = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()
    assert missing["market_data"]["status"] == "incomplete"
    assert missing["market_data"]["missing_symbols"] == ["SPY"]

    store.write_instrument_metadata(
        "SPY", {"con_id": 999, "currency": "USD", "provider": "ibkr"}
    )
    mismatched = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()
    assert mismatched["market_data"]["corrupt_symbols"] == ["SPY"]
    assert mismatched["next_action"] == "sync_market_data"


def test_setup_status_ignores_orphaned_symbol_map_entries_after_sync_manifest(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write_symbol_map({"SPY": 1, "OLD_HOLDING": 99})
    store.write_required_symbols(["SPY"])
    store.write_instrument_metadata(
        "SPY", {"con_id": 1, "currency": "USD", "provider": "ibkr"}
    )
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
    _write_valid_book(
        store,
        valuation_ts="2020-01-01T00:00:00Z",
        positions=[
            {
                "symbol": "SPY",
                "qty": 1,
                "con_id": 1,
                "sec_type": "STK",
                "multiplier": 1,
                "currency": "USD",
            }
        ],
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
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_valid_book(
        store,
        valuation_ts=valuation_ts,
        source="live_ibkr",
        account_fingerprint=_account_fingerprint("U-OLD"),
        broker_mode="paper",
        positions=[
            {
                "symbol": "SPY",
                "qty": 1,
                "con_id": 1,
                "sec_type": "STK",
                "multiplier": 1,
                "currency": "USD",
            }
        ],
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
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_valid_book(
        store,
        valuation_ts=valuation_ts,
        positions=[
            {
                "symbol": "ES",
                "qty": 1,
                "con_id": 99,
                "sec_type": "FUT",
                "multiplier": 50,
                "currency": "USD",
            }
        ],
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


def test_setup_requires_dated_fx_for_a_mixed_currency_book(tmp_path):
    store = BarStore(tmp_path)
    _seed_market(store, datetime.now(timezone.utc))
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_valid_book(
        store,
        valuation_ts=valuation_ts,
        positions=[
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
    )

    response = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    )

    body = response.json()
    assert body["book"]["status"] == "ready"
    assert body["book"]["unsupported_currencies"] == []
    assert body["fx_data"]["status"] == "missing"
    assert body["fx_data"]["required_currencies"] == ["EUR"]
    assert body["next_action"] == "sync_fx_data"

    today = datetime.now(timezone.utc).date()
    sync_ecb_fx(
        store,
        EcbFxProvider(
            fetcher=lambda _url: (
                "CURRENCY,TIME_PERIOD,OBS_VALUE\n"
                f"USD,{today.isoformat()},1.10\n"
            )
        ),
        {"USD", "EUR"},
        today=today,
        years=1,
    )
    ready = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()
    assert ready["fx_data"]["status"] == "ready"
    assert ready["overall"] == "ready"


def test_setup_requires_fx_for_a_non_base_benchmark(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    _write_valid_book(
        store,
        valuation_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=[
            {
                "symbol": "VWRL",
                "qty": 10,
                "con_id": 2,
                "sec_type": "STK",
                "multiplier": 1,
                "currency": "GBP",
                "exchange": "LSE",
            }
        ],
        base_currency="GBP",
    )

    missing = _client(
        store,
        broker=BrokerThatMustNotBeCalled(),
        base_currency="GBP",
    ).get("/api/setup/status").json()

    assert missing["fx_data"]["status"] == "missing"
    assert missing["fx_data"]["required_currencies"] == ["USD"]
    assert missing["fx_data"]["missing_currencies"] == ["USD"]
    assert missing["next_action"] == "sync_fx_data"
    assert missing["overall"] == "needs_attention"


def test_setup_resyncs_when_benchmark_currency_metadata_is_missing(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    store.replace_instrument_metadata({})
    _write_valid_book(
        store,
        valuation_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=[
            {
                "symbol": "VWRL",
                "qty": 10,
                "con_id": 2,
                "sec_type": "STK",
                "multiplier": 1,
                "currency": "GBP",
                "exchange": "LSE",
            }
        ],
        base_currency="GBP",
    )

    body = _client(
        store,
        broker=BrokerThatMustNotBeCalled(),
        base_currency="GBP",
    ).get("/api/setup/status").json()

    assert body["market_data"]["status"] == "ready"
    assert body["fx_data"]["status"] == "missing"
    assert body["next_action"] == "sync_market_data"


def test_setup_routes_to_fx_sync_when_cached_ecb_evidence_is_stale(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    _write_valid_book(
        store,
        valuation_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=[
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
    )
    old_day = now.date() - timedelta(days=8)
    sync_ecb_fx(
        store,
        EcbFxProvider(
            fetcher=lambda _url: (
                "CURRENCY,TIME_PERIOD,OBS_VALUE\n"
                f"USD,{old_day.isoformat()},1.10\n"
            )
        ),
        {"USD", "EUR"},
        today=old_day,
        years=1,
        fetched_at=f"{old_day.isoformat()}T17:00:00Z",
    )

    body = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()

    assert body["fx_data"] == {
        "status": "stale",
        "base_currency": "USD",
        "required_currencies": ["EUR"],
        "missing_currencies": ["EUR"],
        "provider": "ECB",
        "as_of": old_day.isoformat(),
    }
    assert body["next_action"] == "sync_fx_data"
    assert body["overall"] == "needs_attention"


def test_setup_blocks_cross_currency_options_before_analytics(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    _write_valid_book(
        store,
        valuation_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        positions=[
            {
                "symbol": "ASML",
                "qty": 1,
                "con_id": 1,
                "sec_type": "OPT",
                "multiplier": 100,
                "strike": 100,
                "expiry": "20261218",
                "right": "C",
                "currency": "EUR",
                "exchange": "AEB",
            }
        ],
    )

    body = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()

    assert body["overall"] == "needs_attention"
    assert body["book"]["status"] == "unsupported"
    assert body["book"]["reason"] == "cross_currency_option"
    assert body["book"]["unsupported_currencies"] == ["EUR"]
    assert body["next_action"] == "resolve_option_currency"


def test_setup_reports_ucits_profile_readiness_without_blocking_core_cockpit(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    store.write_instrument_metadata(
        "IWDA",
        {
            "provider": "ibkr",
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "MISSING",
            "ucits_profile_reason": "not synced",
        },
    )
    client = _client(store, broker=BrokerThatMustNotBeCalled())
    assert client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 1}]}
    ).status_code == 200

    missing = client.get("/api/setup/status").json()
    assert missing["ucits_data"]["status"] == "incomplete"
    assert missing["ucits_data"]["missing_symbols"] == ["IWDA"]
    assert missing["overall"] == "ready"


def test_setup_reclassifies_an_expired_ucits_profile_as_stale(tmp_path):
    store = BarStore(tmp_path)
    store.write_instrument_metadata(
        "IWDA",
        {
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "FRESH",
        },
    )
    store.write_ucits_profile(
        UcitsEtfProfileV1(
            schema_version="ucits_etf_profile_v1",
            isin="IE00B4L5Y983",
            fund_name="iShares Core MSCI World UCITS ETF USD (Acc)",
            issuer="iShares",
            domicile="Ireland",
            ter_pct=Decimal("0.20"),
            distribution_policy=DistributionPolicy.ACCUMULATING,
            replication_method="Physical",
            benchmark_name="MSCI World",
            provenance=MetadataProvenanceV1(
                source="justetf",
                source_url="https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
                fetched_at_utc=datetime.now(timezone.utc) - timedelta(days=31),
            ),
        )
    )

    ucits = _client(store).get("/api/setup/status").json()["ucits_data"]

    assert ucits["status"] == "stale"
    assert ucits["ready_profiles"] == 0
    assert ucits["stale_symbols"] == ["IWDA"]


def test_setup_does_not_classify_a_us_etf_as_a_ucits_profile_gap(tmp_path):
    store = BarStore(tmp_path)
    now = datetime.now(timezone.utc)
    _seed_market(store, now)
    store.write_instrument_metadata(
        "SPY",
        {
            "provider": "ibkr",
            "currency": "USD",
            "stock_type": "ETF",
            "isin": "US78462F1030",
        },
    )

    status = _client(store, broker=BrokerThatMustNotBeCalled()).get(
        "/api/setup/status"
    ).json()

    assert status["ucits_data"] == {
        "status": "not_required",
        "total_etfs": 0,
        "ready_profiles": 0,
        "missing_symbols": [],
        "stale_symbols": [],
    }
