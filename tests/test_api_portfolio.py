"""API contract tests for GET /api/portfolio (Task B1 — Portfolio truth).

Serialization policy (repo-wide): UTC ISO-Z timestamps, NaN/Inf -> null,
missing/empty book -> structured empty, never a 500.
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
from quantmind.fx import EcbFxProvider, sync_ecb_fx
from quantmind.portfolio import Portfolio, Position


def _flat_bars(price: float, n: int = 30, end: date | None = None) -> pd.DataFrame:
    idx = pd.bdate_range(end=end or date.today(), periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _random_bars(n: int = 300, seed: int = 1, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=date.today(), periods=n)
    close = start * np.abs(np.cumprod(1 + rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


class FakeBroker:
    def __init__(self, portfolio: Portfolio, avg_costs=None, account_summary=None):
        self._portfolio = portfolio
        self._avg_costs = avg_costs
        self._account_summary = account_summary

    async def get_portfolio(self) -> Portfolio:
        return self._portfolio

    async def get_avg_costs(self):
        if self._avg_costs is None:
            return {}
        return self._avg_costs

    async def get_account_summary(self):
        if self._account_summary is None:
            raise RuntimeError("account summary unavailable in this fake")
        return self._account_summary


@pytest.fixture
def store(tmp_path) -> BarStore:
    s = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    s.write_bars(con_id=1, bar_size="1d", bars=_flat_bars(100.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_flat_bars(5.0), meta=meta)
    s.write_bars(con_id=3, bar_size="1d", bars=_flat_bars(380.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "OPT_XYZ": 2, "QQQ": 3})
    for symbol, con_id in (("SPY", 1), ("OPT_XYZ", 2), ("QQQ", 3)):
        s.write_instrument_metadata(
            symbol,
            {"con_id": con_id, "currency": "USD", "exchange": "SMART"},
        )
    return s


@pytest.fixture
def rich_store(tmp_path) -> BarStore:
    """300 business days of random-walk bars (pattern: tests/test_api_risk.py)
    — enough history for a real rolling-beta estimate, unlike `store`'s
    30-bar fixture (which exists precisely to exercise the honest
    insufficient-history degrade path)."""
    s = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    s.write_bars(con_id=1, bar_size="1d", bars=_random_bars(seed=1, start=450.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_random_bars(seed=2, start=380.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "QQQ": 2})
    for symbol, con_id in (("SPY", 1), ("QQQ", 2)):
        s.write_instrument_metadata(
            symbol,
            {"con_id": con_id, "currency": "USD", "exchange": "SMART"},
        )
    return s


def _client(store: BarStore, broker=None, *, base_currency: str = "USD") -> TestClient:
    app = create_app(
        store=store,
        benchmark="SPY",
        api_token="testtoken",
        broker=broker,
        base_currency=base_currency,
    )
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _seed_european_fx(store: BarStore, *, end: date | None = None) -> None:
    as_of = end or date.today()
    csv = f"""CURRENCY,TIME_PERIOD,OBS_VALUE
USD,{as_of.isoformat()},1.1000
GBP,{as_of.isoformat()},0.8800
CHF,{as_of.isoformat()},0.9350
"""
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: csv),
        {"USD", "EUR", "GBP", "CHF"},
        today=as_of,
        years=1,
        fetched_at=f"{as_of.isoformat()}T17:00:00Z",
    )


def _chain_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _write_chain(store: BarStore, underlier: str, rows: list[dict], spot: float, as_of: str | None = None) -> None:
    snapshot_as_of = as_of or str(date.today())
    observed_at = (
        snapshot_as_of
        if "T" in snapshot_as_of
        else f"{snapshot_as_of}T00:00:00Z"
    )
    evidenced_rows = [
        {
            **row,
            "observed_at": row.get("observed_at", observed_at),
            "market_data_type": row.get("market_data_type", 1),
        }
        for row in rows
    ]
    weakest_observed_at = min(
        pd.Timestamp(row["observed_at"]).tz_convert("UTC")
        for row in evidenced_rows
    ).isoformat().replace("+00:00", "Z")
    OptionsStore(store.root).write_chain(
        underlier,
        _chain_df(evidenced_rows),
        OptionsSnapshotMeta(
            as_of=weakest_observed_at,
            spot=spot,
            underlier_con_id=store.read_symbol_map()[underlier],
        ),
    )


def _expiry_str(days_out: int) -> str:
    return (date.today() + timedelta(days=days_out)).strftime("%Y%m%d")


def test_portfolio_no_broker_is_structured_empty(store):
    client = _client(store, broker=None)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["totals"] == {
        "market_value": None,
        "priced_market_value": None,
        "n_positions": 0,
        "priced_positions": 0,
        "valuation_status": "empty",
        "unrealized_pnl": None,
        "reported_unrealized_pnl": None,
        "pnl_status": "empty",
    }
    assert body["base_currency"] == "USD"
    assert body["valuation_ts"].endswith("Z")
    assert body["market_data_as_of"] is None
    assert body["snapshot_id"]


def test_portfolio_empty_book_broker_is_structured_empty(store):
    broker = FakeBroker(Portfolio(positions=(), as_of="2026-07-24"))
    client = _client(store, broker=broker)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["totals"]["n_positions"] == 0
    assert body["totals"]["market_value"] is None


def test_portfolio_two_positions_market_values_and_weights(store):
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),
            Position(con_id=2, symbol="OPT_XYZ", qty=5, sec_type="OPT", multiplier=100.0),
        ),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["n_positions"] == 2

    by_symbol = {p["symbol"]: p for p in body["positions"]}
    spy = by_symbol["SPY"]
    opt = by_symbol["OPT_XYZ"]

    assert spy["last_close"] == pytest.approx(100.0)
    assert spy["market_value"] == pytest.approx(1000.0)  # 10 * 1 * 100

    assert opt["sec_type"] == "OPT"
    assert opt["multiplier"] == pytest.approx(100.0)
    # An option without contract terms and an exact cached-chain quote must
    # never inherit its underlier's 5.0 close as an option premium.
    assert opt["last_close"] is None
    assert opt["market_value"] is None
    assert opt["weight"] is None

    assert body["totals"]["market_value"] is None
    assert body["totals"]["priced_market_value"] == pytest.approx(1000.0)
    assert body["totals"]["valuation_status"] == "partial"
    assert spy["weight"] is None


def test_portfolio_positions_expose_exact_broker_reconciliation_identity(store):
    expiry = _expiry_str(45)
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="SPY",
                qty=10,
                sec_type="STK",
                multiplier=1.0,
                currency="USD",
                exchange="ARCA",
            ),
            Position(
                con_id=4002,
                symbol="OPT_XYZ",
                qty=-2,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="P",
                currency="USD",
                exchange="SMART",
            ),
        ),
        as_of=date.today().isoformat(),
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200
    equity, option = response.json()["positions"]
    assert equity["con_id"] == 1
    assert equity["sec_type"] == "STK"
    assert equity["multiplier"] == pytest.approx(1.0)
    assert equity["exchange"] == "ARCA"
    assert equity["strike"] is None
    assert equity["expiry"] is None
    assert equity["right"] is None
    assert option["con_id"] == 4002
    assert option["sec_type"] == "OPT"
    assert option["multiplier"] == pytest.approx(100.0)
    assert option["exchange"] == "SMART"
    assert option["strike"] == pytest.approx(5.0)
    assert option["expiry"] == expiry
    assert option["right"] == "P"


@pytest.mark.parametrize("held_contract_has_bars", [False, True])
def test_live_portfolio_rejects_a_symbol_map_bound_to_another_stock_contract(
    tmp_path, held_contract_has_bars
):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_symbol_map({"ASML": 1})
    store.write_bars(1, "1d", _flat_bars(399.0), meta)
    if held_contract_has_bars:
        store.write_bars(2, "1d", _flat_bars(800.0), meta)
    store.write_instrument_metadata(
        "ASML", {"con_id": 1, "currency": "USD", "exchange": "NASDAQ"}
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=2,
                symbol="ASML",
                qty=10,
                currency="EUR",
                exchange="AEB",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 409
    assert "ASML" in response.json()["detail"]
    assert "run sync" in response.json()["detail"]


def test_live_portfolio_rejects_dual_listings_that_share_one_ticker(store):
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="ASML", qty=5, currency="EUR", exchange="AEB"),
            Position(
                con_id=99,
                symbol="ASML",
                qty=3,
                currency="USD",
                exchange="NASDAQ",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 422
    assert "multiple listings" in response.json()["detail"]
    assert "ASML" in response.json()["detail"]


def test_live_portfolio_allows_a_broker_stock_absent_from_the_symbol_map(store):
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=999,
                symbol="BROKER_ONLY",
                qty=3,
                currency="USD",
                exchange="SMART",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200, response.text
    position = response.json()["positions"][0]
    assert position["symbol"] == "BROKER_ONLY"
    assert position["last_close"] is None
    assert position["market_value"] is None


def test_live_unmapped_stock_does_not_consume_unbound_cached_currency(store):
    store.write_instrument_metadata(
        "BROKER_ONLY",
        {"con_id": 123, "currency": "EUR", "exchange": "AEB"},
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=999,
                symbol="BROKER_ONLY",
                qty=3,
                currency=None,
                exchange="SMART",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200, response.text
    position = response.json()["positions"][0]
    assert position["currency"] is None
    assert response.json()["fx"]["missing_currencies"] == ["UNKNOWN"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"con_id": 999, "currency": "EUR", "exchange": "AEB"},
        {"currency": "EUR", "exchange": "AEB"},
    ],
    ids=["mismatched-con-id", "missing-con-id"],
)
def test_live_portfolio_rejects_unbound_metadata_currency_fallback(store, metadata):
    stored_metadata = store.read_all_instrument_metadata()
    stored_metadata["SPY"] = metadata
    store.replace_instrument_metadata(stored_metadata)
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="SPY",
                qty=10,
                currency=None,
                exchange="ARCA",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 422
    assert "SPY" in response.json()["detail"]
    assert "metadata contract identity" in response.json()["detail"]


def test_portfolio_position_without_cached_bars_returns_null_price_fields(store):
    store.write_symbol_map({**store.read_symbol_map(), "UNKNOWN": 999})
    store.write_instrument_metadata(
        "UNKNOWN", {"con_id": 999, "currency": "USD", "exchange": "SMART"}
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),
            Position(con_id=999, symbol="UNKNOWN", qty=3, sec_type="STK", multiplier=1.0),
        ),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()

    by_symbol = {p["symbol"]: p for p in body["positions"]}
    unknown = by_symbol["UNKNOWN"]
    assert unknown["last_close"] is None
    assert unknown["market_value"] is None
    assert unknown["weight"] is None

    # known position's market value/weight still computed off the priced subset
    spy = by_symbol["SPY"]
    assert spy["market_value"] == pytest.approx(1000.0)
    assert spy["weight"] is None
    assert body["totals"]["market_value"] is None
    assert body["totals"]["priced_market_value"] == pytest.approx(1000.0)
    assert body["totals"]["valuation_status"] == "partial"


def test_stale_stock_mark_cannot_make_portfolio_valuation_complete(store):
    stale_date = date.today() - timedelta(days=10)
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_flat_bars(100.0, end=stale_date),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST",
            adjusted_asof=stale_date.isoformat(),
        ),
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),
        ),
        as_of="2026-09-04",
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["positions"][0]["last_close"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["totals"]["market_value"] is None
    assert body["totals"]["priced_positions"] == 0
    assert body["totals"]["valuation_status"] == "partial"
    assert body["exposure"] == []


def test_future_stock_mark_cannot_make_portfolio_valuation_complete(store):
    future_date = pd.bdate_range(
        start=date.today() + timedelta(days=1), periods=1
    )[0].date()
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_flat_bars(100.0, end=future_date),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST",
            adjusted_asof=future_date.isoformat(),
        ),
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),
        ),
        as_of=date.today().isoformat(),
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["positions"][0]["last_close"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["totals"]["priced_positions"] == 0
    assert body["totals"]["valuation_status"] == "partial"
    assert body["exposure"] == []


def test_fresh_option_chain_cannot_use_a_stale_underlier_for_greeks(store):
    stale_date = date.today() - timedelta(days=10)
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_flat_bars(100.0, end=stale_date),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST",
            adjusted_asof=stale_date.isoformat(),
        ),
    )
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 4010,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=100.0,
        as_of=str(date.today()),
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()

    assert body["exposure"] == []
    assert body["options_sleeve"]["available"] is False
    assert body["options_sleeve"]["status"] == "unavailable"
    assert "SPY" in body["options_sleeve"]["reason"]
    assert "sync bars" in body["options_sleeve"]["reason"]


def test_fresh_option_chain_cannot_use_a_future_underlier_for_greeks(store):
    future_date = pd.bdate_range(
        start=date.today() + timedelta(days=1), periods=1
    )[0].date()
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=_flat_bars(100.0, end=future_date),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST",
            adjusted_asof=future_date.isoformat(),
        ),
    )
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 4011,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=100.0,
        as_of=str(date.today()),
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()

    assert body["options_sleeve"]["available"] is False
    assert body["options_sleeve"]["status"] == "unavailable"
    assert body["options_sleeve"]["priced_positions"] == 0
    assert "SPY" in body["options_sleeve"]["reason"]
    assert "sync bars" in body["options_sleeve"]["reason"]
    assert body["exposure"] == []


def test_portfolio_corrupt_cached_bars_degrade_to_null_price_fields(store):
    store._path(2, "1d").write_bytes(b"not parquet")
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="OPT_XYZ", qty=3, sec_type="STK"),),
        as_of="2026-07-24",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200
    position = response.json()["positions"][0]
    assert position["last_close"] is None
    assert position["market_value"] is None


def test_portfolio_no_broker_new_sections_are_structured_empty(store):
    client = _client(store, broker=None)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["account"] is None
    assert body["account_note"]
    assert body["exposure"] == []
    assert body["options_sleeve"] == {
        "available": False,
        "status": "unavailable",
        "total_positions": 0,
        "priced_positions": 0,
        "missing_positions": 0,
        "chain_as_of": None,
        "chain_age_days": None,
        "chain_stale": None,
        "reason": "no option positions",
        "underlyings": [],
        "stress_grid": None,
    }
    assert body["expiry_buckets"] == {"le_7d": [], "le_30d": [], "le_90d": [], "later": []}
    assert body["attribution"]["available"] is False
    assert body["attribution"]["reason"]


# --- Ledger essentials: cost basis + unrealized P&L ---


def test_avg_cost_and_unrealized_pnl_computed_from_broker(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    broker = FakeBroker(portfolio, avg_costs={1: 90.0})
    client = _client(store, broker=broker)
    body = client.get("/api/portfolio").json()
    spy = body["positions"][0]
    assert spy["avg_cost"] == pytest.approx(90.0)
    assert spy["unrealized_pnl"] == pytest.approx((100.0 - 90.0) * 10 * 1.0)


def test_avg_cost_missing_from_broker_is_honest_null(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    broker = FakeBroker(portfolio)  # avg_costs defaults to {}
    client = _client(store, broker=broker)
    body = client.get("/api/portfolio").json()
    spy = body["positions"][0]
    assert spy["avg_cost"] is None
    assert spy["unrealized_pnl"] is None


def test_foreign_avg_cost_exposes_local_pnl_but_withholds_base_pnl(store):
    store.write_symbol_map({**store.read_symbol_map(), "ASML": 4})
    store.write_bars(
        con_id=4,
        bar_size="1d",
        bars=_flat_bars(100.0),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())
        ),
    )
    store.write_instrument_metadata(
        "ASML", {"con_id": 4, "currency": "EUR", "exchange": "AEB"}
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=4,
                symbol="ASML",
                qty=10,
                sec_type="STK",
                multiplier=1.0,
                currency="EUR",
            ),
        ),
        as_of="2026-07-24",
    )
    today = date.today()
    sync_ecb_fx(
        store,
        EcbFxProvider(
            fetcher=lambda _url: (
                "CURRENCY,TIME_PERIOD,OBS_VALUE\n"
                f"USD,{today.isoformat()},1.20\n"
            )
        ),
        {"USD", "EUR"},
        today=today,
    )

    body = _client(
        store, broker=FakeBroker(portfolio, avg_costs={4: 90.0})
    ).get("/api/portfolio").json()

    position = body["positions"][0]
    assert position["unrealized_pnl_local"] == pytest.approx(100.0)
    assert position["unrealized_pnl"] is None
    assert body["totals"]["unrealized_pnl"] is None
    assert body["totals"]["reported_unrealized_pnl"] is None
    assert body["totals"]["pnl_status"] == "partial"


# --- Ledger essentials: account summary ---


def test_account_summary_populated_when_broker_supports_it(store):
    portfolio = Portfolio(positions=(), as_of="2026-07-24")
    summary = {
        "net_liquidation": 125000.5,
        "total_cash_value": 20000.0,
        "gross_position_value": 105000.5,
        "buying_power": 60000.0,
        "currency": "USD",
    }
    broker = FakeBroker(portfolio, account_summary=summary)
    client = _client(store, broker=broker)
    body = client.get("/api/portfolio").json()
    assert body["account"] == {
        **summary,
        "source_currency": "USD",
        "net_liquidation_base": summary["net_liquidation"],
        "total_cash_value_base": summary["total_cash_value"],
        "gross_position_value_base": summary["gross_position_value"],
        "buying_power_base": summary["buying_power"],
    }
    assert body["account_note"] is None


def test_account_summary_degrades_honestly_when_broker_lacks_it(store):
    portfolio = Portfolio(positions=(), as_of="2026-07-24")
    broker = FakeBroker(portfolio)  # get_account_summary() raises
    client = _client(store, broker=broker)
    r = client.get("/api/portfolio")
    assert r.status_code == 200  # never-500
    body = r.json()
    assert body["account"] is None
    assert body["account_note"]


def test_account_summary_without_an_explicit_currency_is_withheld_not_rejected(store):
    portfolio = Portfolio(positions=(), as_of="2026-09-04")
    summary = {
        "net_liquidation": 125000.5,
        "total_cash_value": 20000.0,
        "gross_position_value": 105000.5,
        "buying_power": 60000.0,
        "currency": None,
    }

    response = _client(
        store, broker=FakeBroker(portfolio, account_summary=summary)
    ).get("/api/portfolio")

    assert response.status_code == 200
    assert response.json()["account"] is None
    assert "currency" in response.json()["account_note"].lower()


def test_non_usd_account_summary_keeps_local_totals_until_fx_exists(store):
    portfolio = Portfolio(positions=(), as_of="2026-07-24")
    summary = {
        "net_liquidation": 125000.5,
        "total_cash_value": 20000.0,
        "gross_position_value": 105000.5,
        "buying_power": 60000.0,
        "currency": "HKD",
    }

    response = _client(
        store, broker=FakeBroker(portfolio, account_summary=summary)
    ).get("/api/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["currency"] == "HKD"
    assert body["account"]["net_liquidation"] == pytest.approx(125000.5)
    assert body["account"]["net_liquidation_base"] is None
    assert "HKD" in body["account_note"]
    assert "run sync" in body["account_note"].lower()
    assert body["fx"]["status"] == "incomplete"
    assert body["fx"]["missing_currencies"] == ["HKD"]


def test_foreign_account_only_reports_conversion_provenance(store):
    _seed_european_fx(store)
    portfolio = Portfolio(positions=(), as_of=date.today().isoformat())
    summary = {
        "net_liquidation": 100_000.0,
        "total_cash_value": 10_000.0,
        "gross_position_value": 90_000.0,
        "buying_power": 20_000.0,
        "currency": "EUR",
    }

    body = _client(
        store,
        broker=FakeBroker(portfolio, account_summary=summary),
        base_currency="GBP",
    ).get("/api/portfolio").json()

    assert body["account"]["net_liquidation_base"] == pytest.approx(88_000.0)
    assert body["fx"]["status"] == "converted"
    assert body["fx"]["source"] == "ECB"
    assert body["fx"]["as_of"] == date.today().isoformat()
    assert body["fx"]["fetched_at"] == f"{date.today().isoformat()}T17:00:00Z"
    assert body["fx"]["missing_currencies"] == []


def test_disconnected_live_broker_is_not_reported_as_an_empty_portfolio(store):
    client = _client(store, broker=None)
    client.app.state.broker_connection_error = "broker_disconnected"

    response = client.get("/api/portfolio")

    assert response.status_code == 503
    assert "live broker unavailable" in response.json()["detail"]


def test_pinned_live_book_rejects_a_different_current_broker_account(store):
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
    client = _client(store, broker=FakeBroker(portfolio))
    client.app.state.broker_account_id = "DU_ACCOUNT_A"
    client.app.state.broker_mode = "paper"
    pinned = client.post("/api/book/pin", json={}).json()
    client.app.state.broker_account_id = "DU_ACCOUNT_B"

    response = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    )

    assert response.status_code == 409
    assert "account" in response.json()["detail"]


def test_pinned_live_book_rejects_a_different_current_broker_mode(store):
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
    client = _client(store, broker=FakeBroker(portfolio))
    client.app.state.broker_account_id = "DU_ACCOUNT_A"
    client.app.state.broker_mode = "paper"
    pinned = client.post("/api/book/pin", json={}).json()
    client.app.state.broker_mode = "live"

    response = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    )

    assert response.status_code == 409
    assert "mode" in response.json()["detail"]


def test_pinned_book_requires_repin_after_configured_base_currency_changes(store):
    usd_client = _client(store)
    pinned = usd_client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]}
    ).json()

    response = _client(store, base_currency="GBP").get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    )

    assert response.status_code == 409
    assert "re-pin" in response.json()["detail"]
    assert "USD" in response.json()["detail"]
    assert "GBP" in response.json()["detail"]


def test_pinned_portfolio_response_preserves_snapshot_identity_and_valuation_time(store):
    from quantmind.api.routers.book import _account_fingerprint, _pin_and_respond

    valuation_ts = "2026-09-04T01:02:03Z"
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
        as_of=valuation_ts,
    )
    account_id = "DU_ACCOUNT_A"
    pinned = _pin_and_respond(
        store,
        portfolio,
        valuation_ts,
        source="live_ibkr",
        account_fingerprint=_account_fingerprint(account_id),
        broker_mode="paper",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    client.app.state.broker_account_id = account_id
    client.app.state.broker_mode = "paper"

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned.snapshot_id}
    ).json()

    assert body["snapshot_id"] == pinned.snapshot_id
    assert body["valuation_ts"] == valuation_ts
    expected_mark_date = pd.bdate_range(end=date.today(), periods=1)[-1].date().isoformat()
    assert body["market_data_as_of"] == expected_mark_date
    assert body["positions"][0]["mark_as_of"] == expected_mark_date


def test_non_base_live_position_without_fx_is_local_only_and_honestly_partial(store):
    store.write_symbol_map({**store.read_symbol_map(), "ASML": 4})
    store.write_bars(
        con_id=4,
        bar_size="1d",
        bars=_flat_bars(100.0),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())
        ),
    )
    store.write_instrument_metadata(
        "ASML", {"con_id": 4, "currency": "EUR", "exchange": "AEB"}
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=4,
                symbol="ASML",
                qty=10,
                currency="EUR",
                exchange="AEB",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(
        store, broker=FakeBroker(portfolio), base_currency="GBP"
    ).get("/api/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["base_currency"] == "GBP"
    assert body["positions"][0]["currency"] == "EUR"
    assert body["positions"][0]["last_close"] == pytest.approx(100.0)
    assert body["positions"][0]["local_market_value"] == pytest.approx(1000.0)
    assert body["positions"][0]["fx_rate_to_base"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["totals"]["valuation_status"] == "partial"
    assert body["fx"]["status"] == "incomplete"
    assert body["fx"]["missing_currencies"] == ["EUR"]


def test_portfolio_preserves_supported_currency_when_another_has_no_fx_route(
    tmp_path,
):
    store = BarStore(tmp_path)
    bars = _flat_bars(100.0, n=300)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    for con_id in (1, 2, 3):
        store.write_bars(con_id, "1d", bars, meta)
    store.write_symbol_map({"SPY": 1, "EURLEG": 2, "CHFLEG": 3})
    store.write_instrument_metadata(
        "SPY", {"con_id": 1, "currency": "USD", "exchange": "ARCA"}
    )
    store.write_instrument_metadata(
        "EURLEG", {"con_id": 2, "currency": "EUR", "exchange": "AEB"}
    )
    store.write_instrument_metadata(
        "CHFLEG", {"con_id": 3, "currency": "CHF", "exchange": "EBS"}
    )
    fx_rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    fx_rows.extend(
        f"USD,{timestamp.date().isoformat()},1.1000" for timestamp in bars.index
    )
    latest_market_date = bars.index[-1].date()
    fetched_at = f"{latest_market_date.isoformat()}T17:00:00Z"
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(fx_rows)),
        {"USD", "EUR"},
        today=date.today(),
        years=5,
        fetched_at=fetched_at,
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=2, symbol="EURLEG", qty=10, currency="EUR"),
            Position(con_id=3, symbol="CHFLEG", qty=10, currency="CHF"),
        ),
        as_of=date.today().isoformat(),
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200, response.text
    body = response.json()
    positions = {position["symbol"]: position for position in body["positions"]}
    assert positions["EURLEG"]["fx_rate_to_base"] == pytest.approx(1.1)
    assert positions["EURLEG"]["market_value"] == pytest.approx(1_100.0)
    assert positions["CHFLEG"]["local_market_value"] == pytest.approx(1_000.0)
    assert positions["CHFLEG"]["fx_rate_to_base"] is None
    assert positions["CHFLEG"]["market_value"] is None
    assert body["totals"]["market_value"] is None
    assert body["totals"]["priced_market_value"] == pytest.approx(1_100.0)
    assert body["totals"]["priced_positions"] == 1
    assert body["totals"]["valuation_status"] == "partial"
    assert body["fx"]["status"] == "incomplete"
    assert body["fx"]["base_currency"] == "USD"
    assert body["fx"]["source"] == "ECB"
    assert body["fx"]["as_of"] == latest_market_date.isoformat()
    assert body["fx"]["fetched_at"] == fetched_at
    assert body["fx"]["missing_currencies"] == ["CHF"]
    exposure = next(row for row in body["exposure"] if row["underlier"] == "EURLEG")
    assert exposure["fx_rate_to_base"] == pytest.approx(1.1)
    assert exposure["dollar_delta"] == pytest.approx(1_100.0)
    assert body["attribution"]["available"] is False
    assert "CHFLEG" in body["attribution"]["reason"]
    assert "base-currency history" in body["attribution"]["reason"]


def test_european_position_and_account_are_converted_to_selected_base(store):
    latest_market_date = pd.bdate_range(end=date.today(), periods=1)[0].date()
    _seed_european_fx(store, end=latest_market_date)
    store.write_symbol_map({**store.read_symbol_map(), "ASML": 1})
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="ASML",
                qty=10,
                currency="EUR",
                exchange="AEB",
            ),
        ),
        as_of=date.today().isoformat(),
    )
    account = {
        "net_liquidation": 100_000.0,
        "total_cash_value": 10_000.0,
        "gross_position_value": 90_000.0,
        "buying_power": 20_000.0,
        "currency": "EUR",
    }

    response = _client(
        store,
        broker=FakeBroker(portfolio, account_summary=account),
        base_currency="GBP",
    ).get("/api/portfolio")

    assert response.status_code == 200
    body = response.json()
    position = body["positions"][0]
    assert body["base_currency"] == "GBP"
    assert position["currency"] == "EUR"
    assert position["last_close"] == pytest.approx(100.0)  # local quote retained
    assert position["fx_rate_to_base"] == pytest.approx(0.88)
    assert position["local_market_value"] == pytest.approx(1_000.0)
    assert position["market_value"] == pytest.approx(880.0)
    assert body["totals"]["market_value"] == pytest.approx(880.0)
    assert body["fx"]["status"] == "converted"
    assert body["fx"]["source"] == "ECB"
    assert body["account"]["source_currency"] == "EUR"
    assert body["account"]["currency"] == "EUR"
    assert body["account"]["net_liquidation"] == pytest.approx(100_000.0)
    assert body["account"]["net_liquidation_base"] == pytest.approx(88_000.0)
    assert body["exposure"][0]["currency"] == "EUR"
    assert body["exposure"][0]["fx_rate_to_base"] == pytest.approx(0.88)
    assert body["exposure"][0]["dollar_delta"] == pytest.approx(880.0)


def test_spot_valuation_uses_each_market_marks_observation_date_for_fx(store):
    latest_business_date = pd.bdate_range(end=date.today(), periods=1)[0].date()
    older_business_date = pd.bdate_range(
        end=latest_business_date - timedelta(days=1), periods=1
    )[0].date()
    csv = f"""CURRENCY,TIME_PERIOD,OBS_VALUE
USD,{older_business_date.isoformat()},1.1000
GBP,{older_business_date.isoformat()},0.8000
USD,{latest_business_date.isoformat()},1.1000
GBP,{latest_business_date.isoformat()},0.9000
"""
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: csv),
        {"USD", "EUR", "GBP"},
        today=date.today(),
        years=1,
        fetched_at=f"{date.today().isoformat()}T17:00:00Z",
    )
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(
        con_id=4,
        bar_size="1d",
        bars=_flat_bars(100.0, n=1, end=older_business_date),
        meta=meta,
    )
    store.write_bars(
        con_id=5,
        bar_size="1d",
        bars=_flat_bars(100.0, n=1, end=latest_business_date),
        meta=meta,
    )
    store.write_symbol_map({**store.read_symbol_map(), "ASML": 4, "SAP": 5})
    store.write_instrument_metadata("ASML", {"currency": "EUR", "exchange": "AEB"})
    store.write_instrument_metadata("SAP", {"currency": "EUR", "exchange": "IBIS"})
    portfolio = Portfolio(
        positions=(
            Position(con_id=4, symbol="ASML", qty=10, currency="EUR", exchange="AEB"),
            Position(con_id=5, symbol="SAP", qty=10, currency="EUR", exchange="IBIS"),
        ),
        as_of=date.today().isoformat(),
    )

    body = _client(
        store, broker=FakeBroker(portfolio), base_currency="GBP"
    ).get("/api/portfolio").json()

    positions = {position["symbol"]: position for position in body["positions"]}
    assert positions["ASML"]["fx_rate_to_base"] == pytest.approx(0.8)
    assert positions["ASML"]["market_value"] == pytest.approx(800.0)
    assert positions["ASML"]["weight"] == pytest.approx(8 / 17)
    assert positions["SAP"]["fx_rate_to_base"] == pytest.approx(0.9)
    assert positions["SAP"]["market_value"] == pytest.approx(900.0)
    assert positions["SAP"]["weight"] == pytest.approx(9 / 17)
    exposures = {row["underlier"]: row for row in body["exposure"]}
    assert exposures["ASML"]["fx_rate_to_base"] == pytest.approx(0.8)
    assert exposures["ASML"]["dollar_delta"] == pytest.approx(800.0)
    assert exposures["SAP"]["fx_rate_to_base"] == pytest.approx(0.9)
    assert exposures["SAP"]["dollar_delta"] == pytest.approx(900.0)


def test_european_history_is_normalized_before_beta_and_attribution(tmp_path):
    store = BarStore(tmp_path)
    bars = _random_bars(seed=11, start=100.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("IWDA", {"con_id": 2, "currency": "EUR"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in bars.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=date.today(),
        years=5,
        fetched_at=f"{date.today().isoformat()}T17:00:00Z",
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=2, symbol="IWDA", qty=10, currency="EUR"),
        ),
        as_of=date.today().isoformat(),
    )

    body = _client(
        store,
        broker=FakeBroker(portfolio),
        base_currency="GBP",
    ).get("/api/portfolio").json()

    exposure = body["exposure"][0]
    assert exposure["underlier"] == "IWDA"
    assert exposure["beta"] == pytest.approx(1.0, abs=1e-6)
    assert exposure["dollar_delta"] == pytest.approx(
        10 * bars["close"].iloc[-1] * 0.88
    )
    assert body["attribution"]["available"] is True
    assert body["attribution"]["beta"] == pytest.approx(1.0, abs=1e-6)
    assert body["attribution"]["n_obs"] > 0


def test_attribution_is_withheld_when_a_priced_leg_lacks_historical_fx(tmp_path):
    store = BarStore(tmp_path)
    bars = _random_bars(seed=13, start=100.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"SPY": 1, "ASML": 2})
    store.write_instrument_metadata("SPY", {"currency": "USD"})
    store.write_instrument_metadata("ASML", {"currency": "EUR"})
    latest_market_date = bars.index[-1].date()
    sync_ecb_fx(
        store,
        EcbFxProvider(
            fetcher=lambda _url: (
                "CURRENCY,TIME_PERIOD,OBS_VALUE\n"
                f"USD,{latest_market_date.isoformat()},1.1000\n"
            )
        ),
        {"USD", "EUR"},
        today=date.today(),
        years=1,
    )
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, currency="USD"),
            Position(con_id=2, symbol="ASML", qty=10, currency="EUR"),
        ),
        as_of=date.today().isoformat(),
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["totals"]["valuation_status"] == "complete"
    assert body["attribution"]["available"] is False
    assert "ASML" in body["attribution"]["reason"]
    assert "base-currency history" in body["attribution"]["reason"]


def test_attribution_is_withheld_when_any_book_leg_has_only_a_stale_mark(tmp_path):
    store = BarStore(tmp_path)
    fresh = _random_bars(seed=17, start=100.0)
    stale = _random_bars(seed=18, start=80.0).iloc[:-10]
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", fresh, meta)
    store.write_bars(2, "1d", stale, meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    store.write_instrument_metadata("SPY", {"currency": "USD"})
    store.write_instrument_metadata("QQQ", {"currency": "USD"})
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10, currency="USD"),
            Position(con_id=2, symbol="QQQ", qty=10, currency="USD"),
        ),
        as_of=date.today().isoformat(),
    )

    body = _client(store, broker=FakeBroker(portfolio)).get(
        "/api/portfolio", params={"attribution_days": 90}
    ).json()

    assert body["totals"]["valuation_status"] == "partial"
    assert body["attribution"]["available"] is False
    assert "QQQ" in body["attribution"]["reason"]
    assert "base-currency history" in body["attribution"]["reason"]


def test_exposure_loads_fx_for_a_foreign_benchmark_not_held_in_the_book(tmp_path):
    store = BarStore(tmp_path)
    bars = _random_bars(seed=14, start=100.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"UKBENCH": 1, "IWDA": 2})
    store.write_instrument_metadata(
        "UKBENCH", {"con_id": 1, "currency": "GBP"}
    )
    store.write_instrument_metadata("IWDA", {"con_id": 2, "currency": "EUR"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in bars.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=date.today(),
        years=5,
        fetched_at=f"{date.today().isoformat()}T17:00:00Z",
    )
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="IWDA", qty=10, currency="EUR"),),
        as_of=date.today().isoformat(),
    )
    app = create_app(
        store=store,
        benchmark="UKBENCH",
        api_token="testtoken",
        broker=FakeBroker(portfolio),
        base_currency="USD",
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    )

    response = client.get("/api/portfolio")

    assert response.status_code == 200, response.text
    exposure = response.json()["exposure"][0]
    assert exposure["underlier"] == "IWDA"
    assert exposure["beta"] == pytest.approx(1.0, abs=1e-6)
    assert exposure["beta_note"] is None


def test_exposure_and_attribution_fail_closed_without_benchmark_currency(tmp_path):
    store = BarStore(tmp_path)
    bars = _random_bars(seed=15, start=100.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2})
    store.write_instrument_metadata("IWDA", {"currency": "EUR"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    for timestamp in bars.index:
        day = timestamp.date().isoformat()
        rows.extend([f"USD,{day},1.1000", f"GBP,{day},0.8800"])
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR", "GBP"},
        today=date.today(),
        years=5,
    )
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="IWDA", qty=10, currency="EUR"),),
        as_of=date.today().isoformat(),
    )

    body = _client(
        store,
        broker=FakeBroker(portfolio),
        base_currency="GBP",
    ).get("/api/portfolio").json()

    exposure = body["exposure"][0]
    assert exposure["beta"] is None
    assert "benchmark SPY currency metadata" in exposure["beta_note"]
    assert body["attribution"]["available"] is False
    assert "benchmark SPY currency metadata" in body["attribution"]["reason"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"con_id": 999, "currency": "USD"},
        {"currency": "USD"},
    ],
    ids=["mismatched-con-id", "missing-con-id"],
)
def test_exposure_and_attribution_fail_closed_with_unbound_benchmark_metadata(
    rich_store, metadata
):
    stored_metadata = rich_store.read_all_instrument_metadata()
    stored_metadata["SPY"] = metadata
    rich_store.replace_instrument_metadata(stored_metadata)
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="QQQ", qty=10, currency="USD"),),
        as_of=date.today().isoformat(),
    )

    response = _client(rich_store, broker=FakeBroker(portfolio)).get(
        "/api/portfolio", params={"attribution_days": 90}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exposure"][0]["beta"] is None
    assert "benchmark SPY" in body["exposure"][0]["beta_note"]
    assert "metadata contract identity" in body["exposure"][0]["beta_note"]
    assert "run sync" in body["exposure"][0]["beta_note"]
    assert body["attribution"]["available"] is False
    assert "benchmark SPY" in body["attribution"]["reason"]
    assert "metadata contract identity" in body["attribution"]["reason"]


def test_exposure_and_attribution_fail_closed_without_benchmark_fx(tmp_path):
    store = BarStore(tmp_path)
    bars = _random_bars(seed=16, start=100.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars, meta)
    store.write_bars(2, "1d", bars.copy(), meta)
    store.write_symbol_map({"UKBENCH": 1, "SPY": 2})
    store.write_instrument_metadata(
        "UKBENCH", {"con_id": 1, "currency": "GBP"}
    )
    store.write_instrument_metadata("SPY", {"con_id": 2, "currency": "USD"})
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="SPY", qty=10, currency="USD"),),
        as_of=date.today().isoformat(),
    )
    app = create_app(
        store=store,
        benchmark="UKBENCH",
        api_token="testtoken",
        broker=FakeBroker(portfolio),
        base_currency="USD",
    )

    body = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer testtoken"},
    ).get("/api/portfolio").json()

    exposure = body["exposure"][0]
    assert exposure["beta"] is None
    assert "benchmark UKBENCH FX evidence" in exposure["beta_note"]
    assert body["attribution"]["available"] is False
    assert "benchmark UKBENCH FX evidence" in body["attribution"]["reason"]


def test_exposure_and_attribution_fail_closed_with_a_stale_benchmark(tmp_path):
    store = BarStore(tmp_path)
    benchmark = _random_bars(seed=19, start=100.0).iloc[:-10]
    position = _random_bars(seed=20, start=80.0)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", benchmark, meta)
    store.write_bars(2, "1d", position, meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    store.write_instrument_metadata("SPY", {"currency": "USD"})
    store.write_instrument_metadata("QQQ", {"currency": "USD"})
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="QQQ", qty=10, currency="USD"),),
        as_of=date.today().isoformat(),
    )

    body = _client(store, broker=FakeBroker(portfolio)).get(
        "/api/portfolio", params={"attribution_days": 90}
    ).json()

    assert body["exposure"][0]["beta"] is None
    assert "benchmark SPY cached bars are stale" in body["exposure"][0]["beta_note"]
    assert body["attribution"]["available"] is False
    assert "benchmark SPY cached bars are stale" in body["attribution"]["reason"]


def test_unsupported_live_security_type_is_blocked(store):
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=1,
                symbol="ES",
                qty=1,
                sec_type="FUT",
                multiplier=50,
                currency="USD",
            ),
        ),
        as_of="2026-09-04",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 422
    assert "FUT" in response.json()["detail"]


def test_option_with_unknown_currency_is_refused_before_monetary_aggregation(store):
    store.write_symbol_map({**store.read_symbol_map(), "NOCURR": 2})
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=2,
                symbol="NOCURR",
                qty=1,
                sec_type="OPT",
                multiplier=100,
                currency=None,
            ),
        ),
        as_of=date.today().isoformat(),
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 422
    assert "UNKNOWN" in response.json()["detail"]


def test_unsupported_pinned_security_type_is_not_reconstructed_as_stock(store):
    from quantmind.api.routers.book import _pin_and_respond

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

    response = _client(store).get(
        "/api/portfolio", params={"book_ref": pinned.snapshot_id}
    )

    assert response.status_code == 422
    assert "FUT" in response.json()["detail"]


# --- Delta-adjusted exposure ---


def test_exposure_underlier_equal_to_benchmark_has_beta_one(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    assert len(body["exposure"]) == 1
    exp = body["exposure"][0]
    assert exp["underlier"] == "SPY"
    assert exp["spot"] == pytest.approx(100.0)
    assert exp["net_delta"] == pytest.approx(10.0)
    assert exp["dollar_delta"] == pytest.approx(1000.0)
    assert exp["beta"] == pytest.approx(1.0)
    assert exp["spy_equivalent_notional"] == pytest.approx(1000.0)


def test_exposure_beta_none_when_insufficient_history(store):
    # `store` has only 30 bars for QQQ/SPY — below the 60-window + 2 floor —
    # so beta must degrade to None (with a note), never a fabricated number.
    portfolio = Portfolio(
        positions=(Position(con_id=3, symbol="QQQ", qty=5, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    exp = body["exposure"][0]
    assert exp["underlier"] == "QQQ"
    assert exp["net_delta"] == pytest.approx(5.0)
    assert exp["beta"] is None
    assert exp["spy_equivalent_notional"] is None
    assert exp["beta_note"]


def test_exposure_beta_computed_with_sufficient_history(rich_store):
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="QQQ", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(rich_store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    exp = body["exposure"][0]
    assert exp["beta"] is not None
    assert np.isfinite(exp["beta"])
    assert exp["spy_equivalent_notional"] == pytest.approx(exp["dollar_delta"] * exp["beta"])


def test_exposure_skips_underlier_with_no_cached_bars(store):
    store.write_symbol_map({**store.read_symbol_map(), "UNKNOWN": 999})
    store.write_instrument_metadata(
        "UNKNOWN", {"con_id": 999, "currency": "USD", "exchange": "SMART"}
    )
    portfolio = Portfolio(
        positions=(Position(con_id=999, symbol="UNKNOWN", qty=3, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    assert r.json()["exposure"] == []


# --- Options sleeve panel ---


def test_options_sleeve_no_option_positions(store):
    portfolio = Portfolio(
        positions=(Position(con_id=1, symbol="SPY", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    assert body["options_sleeve"]["available"] is False
    assert body["options_sleeve"]["reason"] == "no option positions"


def test_options_sleeve_live_broker_opt_position_has_no_strike_expiry_degrades_honestly(store):
    # Live-broker OPT positions never carry strike/expiry (Position has no
    # room for them — Engineering Constraint 9's one Portfolio type; see
    # routers/book.py's read_book_positions docstring for the same limit) —
    # so even with an OPT sec_type position present, the sleeve can't price
    # it and must say so honestly rather than silently omit it.
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="OPT_XYZ", qty=5, sec_type="OPT", multiplier=100.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    sleeve = body["options_sleeve"]
    assert sleeve["available"] is False
    assert sleeve["status"] == "unavailable"
    assert sleeve["total_positions"] == 1
    assert sleeve["priced_positions"] == 0
    assert sleeve["missing_positions"] == 1
    assert sleeve["chain_as_of"] is None
    assert sleeve["chain_age_days"] is None
    assert sleeve["chain_stale"] is None
    assert sleeve["reason"] == "chain not ingested — run options_sync"


def test_options_sleeve_prices_a_live_broker_option_when_contract_terms_are_complete(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "OPT_XYZ",
        [
            {
                "expiry": expiry,
                "strike": 5.0,
                "right": "C",
                "con_id": 2,
                "bid": 0.5,
                "ask": 0.6,
                "iv": 0.35,
                "delta": 0.55,
                "multiplier": 100.0,
            }
        ],
        spot=5.0,
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=2,
                symbol="OPT_XYZ",
                qty=5,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="C",
                currency="USD",
            ),
        ),
        as_of="2026-07-24",
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["options_sleeve"]["available"] is True
    assert body["options_sleeve"]["underlyings"][0]["underlier"] == "OPT_XYZ"
    assert body["expiry_buckets"]["le_90d"][0]["expiry"] == expiry


def test_corrupt_option_chain_degrades_to_unavailable_sleeve(store):
    expiry = _expiry_str(45)
    chain_path = OptionsStore(store.root)._path("OPT_XYZ")
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    chain_path.write_bytes(b"not parquet")
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=2,
                symbol="OPT_XYZ",
                qty=5,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="C",
                currency="USD",
            ),
        ),
        as_of="2026-07-24",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200
    sleeve = response.json()["options_sleeve"]
    assert sleeve["status"] == "unavailable"
    assert sleeve["priced_positions"] == 0
    assert sleeve["missing_positions"] == 1


def test_live_option_prefers_held_contract_con_id_when_terms_are_ambiguous(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "OPT_XYZ",
        [
            {
                "expiry": expiry,
                "strike": 5.0,
                "right": "C",
                "con_id": 4001,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            },
            {
                "expiry": expiry,
                "strike": 5.0,
                "right": "C",
                "con_id": 4002,
                "bid": 3.0,
                "ask": 3.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            },
        ],
        spot=5.0,
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=4002,
                symbol="OPT_XYZ",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="C",
                currency="USD",
            ),
        ),
        as_of="2026-07-24",
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["positions"][0]["last_close"] == pytest.approx(3.1)


def test_live_option_does_not_fall_back_to_same_terms_when_contract_id_differs(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "OPT_XYZ",
        [
            {
                "expiry": expiry,
                "strike": 5.0,
                "right": "C",
                "con_id": 4001,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=5.0,
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=4999,
                symbol="OPT_XYZ",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="C",
                currency="USD",
            ),
        ),
        as_of="2026-09-04",
    )

    body = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio").json()

    assert body["positions"][0]["last_close"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["options_sleeve"]["status"] == "unavailable"


def test_manual_option_rejects_ambiguous_terms_without_matching_contract_id(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 4001,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            },
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 4002,
                "bid": 3.0,
                "ask": 3.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            },
        ],
        spot=100.0,
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                    "currency": "USD",
                }
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()

    assert body["positions"][0]["last_close"] is None
    assert body["options_sleeve"]["status"] == "unavailable"


def test_manual_option_rejects_chain_bound_to_an_old_underlier_contract(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 4001,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=100.0,
    )
    store.write_bars(
        con_id=99,
        bar_size="1d",
        bars=_flat_bars(120.0),
        meta=BarMeta(
            bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())
        ),
    )
    store.write_symbol_map({**store.read_symbol_map(), "SPY": 99})
    store.write_instrument_metadata(
        "SPY", {"con_id": 99, "currency": "USD", "exchange": "ARCA"}
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    )
    assert pinned.status_code == 200

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned.json()["snapshot_id"]}
    ).json()

    assert body["positions"][0]["last_close"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["options_sleeve"]["status"] == "unavailable"


def test_zero_iv_option_degrades_to_unavailable_sleeve(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "OPT_XYZ",
        [
            {
                "expiry": expiry,
                "strike": 5.0,
                "right": "C",
                "con_id": 4002,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.0,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=5.0,
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=4002,
                symbol="OPT_XYZ",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=5.0,
                expiry=expiry,
                right="C",
                currency="USD",
            ),
        ),
        as_of="2026-07-24",
    )

    response = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")

    assert response.status_code == 200
    assert response.json()["options_sleeve"]["status"] == "unavailable"


def test_options_sleeve_populated_via_book_ref_with_chain_data(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry, "strike": 105.0, "right": "C", "con_id": 1001,
                "bid": 3.0, "ask": 3.2, "iv": 0.3, "delta": 0.5, "multiplier": 100.0,
            }
        ],
        spot=100.0,
    )
    client = _client(store, broker=None)
    pin = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 2, "strike": 105.0, "expiry": expiry, "right": "C"}]},
    )
    assert pin.status_code == 200
    snapshot_id = pin.json()["snapshot_id"]

    r = client.get("/api/portfolio", params={"book_ref": snapshot_id})
    assert r.status_code == 200
    body = r.json()
    sleeve = body["options_sleeve"]
    assert sleeve["available"] is True
    assert sleeve["status"] == "complete"
    assert sleeve["total_positions"] == 1
    assert sleeve["priced_positions"] == 1
    assert sleeve["missing_positions"] == 0
    assert sleeve["chain_as_of"] == f"{date.today().isoformat()}T00:00:00Z"
    assert sleeve["chain_age_days"] == 0
    assert sleeve["chain_stale"] is False
    assert sleeve["reason"] is None
    assert len(sleeve["underlyings"]) == 1
    assert sleeve["underlyings"][0]["underlier"] == "SPY"
    assert sleeve["stress_grid"] is not None
    assert len(sleeve["stress_grid"]["pnl"]) == len(sleeve["stress_grid"]["vol_shocks"])

    # exposure section also reflects the option leg's delta, not just shares
    exp = next(e for e in body["exposure"] if e["underlier"] == "SPY")
    assert exp["net_delta"] is not None
    assert exp["net_delta"] != pytest.approx(0.0)


def test_multi_leg_book_ref_values_each_option_at_its_cached_midpoint(store):
    # A pinned option book uses the underlier's conId only to find history.
    # Its current marks come from each exact chain row: both rows quote
    # 1.0 / 1.2, so each contract is worth the hand-derived midpoint 1.1,
    # never SPY's 100.0 close and never another leg's quote.
    expiry = _expiry_str(45)
    rows = [
        {
            "expiry": expiry, "strike": strike, "right": "C", "con_id": 3000 + i,
            "bid": 1.0, "ask": 1.2, "iv": 0.25, "delta": 0.5, "multiplier": 100.0,
        }
        for i, strike in enumerate((105.0, 110.0))
    ]
    _write_chain(store, "SPY", rows, spot=100.0)

    client = _client(store, broker=None)
    pin = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {"symbol": "SPY", "qty": 1, "strike": 105.0, "expiry": expiry, "right": "C"},
                {"symbol": "SPY", "qty": 5, "strike": 110.0, "expiry": expiry, "right": "C"},
            ]
        },
    )
    assert pin.status_code == 200
    snapshot_id = pin.json()["snapshot_id"]

    body = client.get("/api/portfolio", params={"book_ref": snapshot_id}).json()
    assert len(body["positions"]) == 2

    by_qty = {p["qty"]: p for p in body["positions"]}
    assert by_qty[1]["last_close"] == pytest.approx(1.1)
    assert by_qty[5]["last_close"] == pytest.approx(1.1)
    assert by_qty[1]["market_value"] == pytest.approx(1 * 100.0 * 1.1)
    assert by_qty[5]["market_value"] == pytest.approx(5 * 100.0 * 1.1)
    assert body["totals"]["market_value"] == pytest.approx(660.0)
    assert by_qty[1]["weight"] == pytest.approx(1 / 6)
    assert by_qty[5]["weight"] == pytest.approx(5 / 6)


@pytest.mark.parametrize(
    ("bid", "ask", "expected_mark"),
    [(0.8, None, 0.8), (None, 1.25, 1.25)],
)
def test_book_ref_option_uses_the_available_one_sided_quote(
    store, bid, ask, expected_mark
):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 3000,
                "bid": bid,
                "ask": ask,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=100.0,
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 2,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()

    assert body["positions"][0]["last_close"] == pytest.approx(expected_mark)
    assert body["positions"][0]["market_value"] == pytest.approx(
        2 * 100 * expected_mark
    )


def test_options_sleeve_reports_partial_when_a_pinned_leg_has_no_exact_quote(store):
    expiry = _expiry_str(45)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 3000,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=100.0,
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                },
                {
                    "symbol": "SPY",
                    "qty": 2,
                    "strike": 110.0,
                    "expiry": expiry,
                    "right": "C",
                },
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()
    sleeve = body["options_sleeve"]

    assert sleeve["available"] is True
    assert sleeve["status"] == "partial"
    assert sleeve["total_positions"] == 2
    assert sleeve["priced_positions"] == 1
    assert sleeve["missing_positions"] == 1
    assert "1 of 2" in sleeve["reason"]
    by_qty = {p["qty"]: p for p in body["positions"]}
    assert by_qty[1]["last_close"] == pytest.approx(1.1)
    assert by_qty[2]["last_close"] is None
    assert by_qty[2]["market_value"] is None


def test_options_sleeve_surfaces_stale_chain_without_reporting_complete(store):
    expiry = _expiry_str(45)
    chain_date = date.today() - timedelta(days=10)
    _write_chain(
        store,
        "SPY",
        [
            {
                "expiry": expiry,
                "strike": 105.0,
                "right": "C",
                "con_id": 3000,
                "bid": 1.0,
                "ask": 1.2,
                "iv": 0.25,
                "delta": 0.5,
                "multiplier": 100.0,
                "observed_at": f"{chain_date.isoformat()}T20:00:00Z",
                "market_data_type": 4,
            }
        ],
        spot=100.0,
        as_of=f"{date.today().isoformat()}T20:00:00Z",
    )
    client = _client(store, broker=None)
    pinned = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 1,
                    "strike": 105.0,
                    "expiry": expiry,
                    "right": "C",
                }
            ]
        },
    ).json()

    body = client.get(
        "/api/portfolio", params={"book_ref": pinned["snapshot_id"]}
    ).json()
    sleeve = body["options_sleeve"]

    assert sleeve["available"] is False
    assert sleeve["status"] == "unavailable"
    assert sleeve["priced_positions"] == 0
    assert sleeve["chain_as_of"] == f"{chain_date.isoformat()}T20:00:00Z"
    assert sleeve["chain_age_days"] == int(
        np.busday_count(chain_date.isoformat(), date.today().isoformat())
    )
    assert sleeve["chain_stale"] is True
    assert "stale" in sleeve["reason"]
    assert sleeve["underlyings"] == []
    assert sleeve["stress_grid"] is None
    assert body["exposure"] == []
    assert body["positions"][0]["last_close"] is None
    assert body["totals"]["valuation_status"] == "partial"


def test_options_sleeve_distinct_reason_when_option_underlier_not_in_cache(store):
    # Fix-round-1 MINOR: an OPT position whose underlier has no symbol-map
    # entry (or no cached bars) used to fall through to the generic "chain
    # not ingested" reason — it must name the real problem instead.
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=555,
                symbol="NOPE",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                currency="USD",
            ),
        ),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    sleeve = body["options_sleeve"]
    assert sleeve["available"] is False
    assert "NOPE" in sleeve["reason"]
    assert sleeve["reason"] != "chain not ingested — run options_sync"


# --- Expiry buckets ---


def test_expiry_buckets_categorize_option_legs_by_days_to_expiry(store):
    rows = []
    positions = []
    for i, days_out in enumerate((5, 20, 60, 200)):
        expiry = _expiry_str(days_out)
        strike = 100.0 + i
        rows.append(
            {
                "expiry": expiry, "strike": strike, "right": "C", "con_id": 2000 + i,
                "bid": 1.0, "ask": 1.2, "iv": 0.25, "delta": 0.5, "multiplier": 100.0,
            }
        )
        positions.append({"symbol": "SPY", "qty": 1, "strike": strike, "expiry": expiry, "right": "C"})
    _write_chain(store, "SPY", rows, spot=100.0)

    client = _client(store, broker=None)
    pin = client.post("/api/book/pin", json={"positions": positions})
    assert pin.status_code == 200
    snapshot_id = pin.json()["snapshot_id"]

    body = client.get("/api/portfolio", params={"book_ref": snapshot_id}).json()
    buckets = body["expiry_buckets"]
    assert len(buckets["le_7d"]) == 1
    assert len(buckets["le_30d"]) == 1
    assert len(buckets["le_90d"]) == 1
    assert len(buckets["later"]) == 1
    assert buckets["le_7d"][0]["symbol"] == "SPY"


# --- Core-vs-overlay P&L attribution ---


def test_attribution_unavailable_with_insufficient_history(store):
    portfolio = Portfolio(
        positions=(Position(con_id=3, symbol="QQQ", qty=5, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio").json()
    assert body["attribution"]["available"] is False
    assert body["attribution"]["reason"]
    assert body["attribution"]["series"] == []


def test_attribution_available_with_sufficient_history(rich_store):
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="QQQ", qty=10, sec_type="STK", multiplier=1.0),),
        as_of="2026-07-24",
    )
    client = _client(rich_store, broker=FakeBroker(portfolio))
    body = client.get("/api/portfolio", params={"attribution_days": 90}).json()
    attribution = body["attribution"]
    assert attribution["available"] is True
    assert attribution["reason"] is None
    assert attribution["beta"] is not None
    assert attribution["n_obs"] > 0
    assert attribution["n_obs"] <= 90
    assert attribution["total_pnl"] == pytest.approx(
        attribution["core_pnl"] + attribution["overlay_pnl"], rel=1e-6
    )
    assert len(attribution["series"]) == attribution["n_obs"]
    for point in attribution["series"]:
        assert point["date"].endswith("Z")
        assert point["total_pnl"] == pytest.approx(point["core_pnl"] + point["overlay_pnl"], abs=1e-6)


def test_attribution_aligns_price_levels_before_cross_calendar_returns(tmp_path):
    index = pd.bdate_range(end=date.today(), periods=601)
    rng = np.random.default_rng(909)
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
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today()))
    store.write_bars(1, "1d", bars(close), meta)
    store.write_bars(2, "1d", bars(close.iloc[::2]), meta)
    store.write_symbol_map({"SPY": 1, "BOOK": 2})
    for con_id, symbol in enumerate(("SPY", "BOOK"), 1):
        store.write_instrument_metadata(
            symbol, {"con_id": con_id, "currency": "USD"}
        )
    portfolio = Portfolio(
        positions=(
            Position(con_id=2, symbol="BOOK", qty=10, currency="USD"),
        ),
        as_of=date.today().isoformat(),
    )

    response = _client(store, broker=FakeBroker(portfolio)).get(
        "/api/portfolio", params={"attribution_days": 252}
    )

    assert response.status_code == 200, response.text
    attribution = response.json()["attribution"]
    assert attribution["available"] is True
    assert attribution["beta"] == pytest.approx(1.0, abs=1e-9)


def test_attribution_is_unavailable_for_option_book_without_option_price_history(rich_store):
    expiry = _expiry_str(45)
    _write_chain(
        rich_store,
        "QQQ",
        [
            {
                "expiry": expiry,
                "strike": 380.0,
                "right": "C",
                "con_id": 2,
                "bid": 4.0,
                "ask": 4.4,
                "iv": 0.3,
                "delta": 0.5,
                "multiplier": 100.0,
            }
        ],
        spot=380.0,
    )
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=2,
                symbol="QQQ",
                qty=1,
                sec_type="OPT",
                multiplier=100.0,
                strike=380.0,
                expiry=expiry,
                right="C",
            ),
        ),
        as_of="2026-07-24",
    )

    body = _client(rich_store, broker=FakeBroker(portfolio)).get(
        "/api/portfolio", params={"attribution_days": 90}
    ).json()

    assert body["options_sleeve"]["status"] == "complete"
    attribution = body["attribution"]
    assert attribution["available"] is False
    assert "option price history" in attribution["reason"]
    assert attribution["series"] == []
