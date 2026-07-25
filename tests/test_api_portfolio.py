"""API contract tests for GET /api/portfolio (Task 1 — Portfolio page).

Serialization policy (repo-wide): UTC ISO-Z timestamps, NaN/Inf -> null,
missing/empty book -> structured empty, never a 500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.portfolio import Portfolio, Position


def _flat_bars(price: float, n: int = 30) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
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
    s.write_bars(con_id=1, bar_size="1d", bars=_flat_bars(100.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_flat_bars(5.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "OPT_XYZ": 2})
    return s


def _client(store: BarStore, broker=None) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", broker=broker)
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_portfolio_no_broker_is_structured_empty(store):
    client = _client(store, broker=None)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["totals"] == {"market_value": None, "n_positions": 0}
    assert body["base_currency"] == "USD"
    assert body["valuation_ts"].endswith("Z")
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
    assert opt["last_close"] == pytest.approx(5.0)
    assert opt["market_value"] == pytest.approx(2500.0)  # 5 * 100 * 5

    total_mv = 1000.0 + 2500.0
    assert body["totals"]["market_value"] == pytest.approx(total_mv)
    assert spy["weight"] == pytest.approx(1000.0 / total_mv)
    assert opt["weight"] == pytest.approx(2500.0 / total_mv)


def test_portfolio_position_without_cached_bars_returns_null_price_fields(store):
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
    assert spy["weight"] == pytest.approx(1.0)  # only priced position -> full weight
    assert body["totals"]["market_value"] == pytest.approx(1000.0)
