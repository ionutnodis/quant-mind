"""API contract tests for GET /api/portfolio (Task B1 — Portfolio truth).

Serialization policy (repo-wide): UTC ISO-Z timestamps, NaN/Inf -> null,
missing/empty book -> structured empty, never a 500.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.portfolio import Portfolio, Position


def _flat_bars(price: float, n: int = 30) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _random_bars(n: int = 300, seed: int = 1, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
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
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    s.write_bars(con_id=1, bar_size="1d", bars=_flat_bars(100.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_flat_bars(5.0), meta=meta)
    s.write_bars(con_id=3, bar_size="1d", bars=_flat_bars(380.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "OPT_XYZ": 2, "QQQ": 3})
    return s


@pytest.fixture
def rich_store(tmp_path) -> BarStore:
    """300 business days of random-walk bars (pattern: tests/test_api_risk.py)
    — enough history for a real rolling-beta estimate, unlike `store`'s
    30-bar fixture (which exists precisely to exercise the honest
    insufficient-history degrade path)."""
    s = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    s.write_bars(con_id=1, bar_size="1d", bars=_random_bars(seed=1, start=450.0), meta=meta)
    s.write_bars(con_id=2, bar_size="1d", bars=_random_bars(seed=2, start=380.0), meta=meta)
    s.write_symbol_map({"SPY": 1, "QQQ": 2})
    return s


def _client(store: BarStore, broker=None) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", broker=broker)
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _chain_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _write_chain(store: BarStore, underlier: str, rows: list[dict], spot: float, as_of: str | None = None) -> None:
    OptionsStore(store.root).write_chain(
        underlier, _chain_df(rows), OptionsSnapshotMeta(as_of=as_of or str(date.today()), spot=spot)
    )


def _expiry_str(days_out: int) -> str:
    return (date.today() + timedelta(days=days_out)).strftime("%Y%m%d")


def test_totals_disclose_mixed_position_currencies(store):
    # Live-account incident 2026-07-27: a GBP-based book holding LSE UCITS
    # (GBP bars) alongside US names (USD bars) — totals silently summed
    # unconverted native amounts. Until FX-aware valuation lands, mixed
    # currencies MUST be disclosed on the response, never silent.
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("QQQ", {"con_id": 3, "currency": "GBP"})
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10),
            Position(con_id=3, symbol="QQQ", qty=5),
        ),
        as_of="2026-07-27",
    )
    r = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")
    assert r.status_code == 200
    note = r.json()["totals_note"]
    assert note is not None
    assert "GBP" in note and "USD" in note
    assert "unconverted" in note


def test_totals_note_is_null_for_a_single_currency_book(store):
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("QQQ", {"con_id": 3, "currency": "USD"})
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="SPY", qty=10),
            Position(con_id=3, symbol="QQQ", qty=5),
        ),
        as_of="2026-07-27",
    )
    r = _client(store, broker=FakeBroker(portfolio)).get("/api/portfolio")
    assert r.status_code == 200
    assert r.json()["totals_note"] is None


def test_portfolio_no_broker_is_structured_empty(store):
    client = _client(store, broker=None)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["totals"] == {"market_value": None, "n_positions": 0, "unrealized_pnl": None}
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


# --- Ledger essentials: account summary ---


def test_account_summary_populated_when_broker_supports_it(store):
    portfolio = Portfolio(positions=(), as_of="2026-07-24")
    summary = {
        "net_liquidation": 125000.5,
        "total_cash_value": 20000.0,
        "gross_position_value": 105000.5,
        "buying_power": 60000.0,
    }
    broker = FakeBroker(portfolio, account_summary=summary)
    client = _client(store, broker=broker)
    body = client.get("/api/portfolio").json()
    assert body["account"] == summary
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
    assert body["options_sleeve"]["available"] is False
    assert body["options_sleeve"]["reason"] == "chain not ingested — run options_sync"


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
    assert sleeve["reason"] is None
    assert len(sleeve["underlyings"]) == 1
    assert sleeve["underlyings"][0]["underlier"] == "SPY"
    assert sleeve["stress_grid"] is not None
    assert len(sleeve["stress_grid"]["pnl"]) == len(sleeve["stress_grid"]["vol_shocks"])

    # exposure section also reflects the option leg's delta, not just shares
    exp = next(e for e in body["exposure"] if e["underlier"] == "SPY")
    assert exp["net_delta"] is not None
    assert exp["net_delta"] != pytest.approx(0.0)


def test_multi_leg_book_ref_market_values_are_per_position_not_per_conid(store):
    # Fix-round-1 CRITICAL (reviewer live repro): a book_ref book's legs all
    # share the synthetic con_id (= symbol_map[underlier]), so any con_id-
    # keyed dict in the ledger path collapses a multi-leg book — a 2-leg SPY
    # book (qty 1 @ 105c, qty 5 @ 110c, underlier close 100.0) reported BOTH
    # positions as market_value 50000.0 / weight 1.0 and totals 50000.0.
    # Correct: 10000 / 50000, weights 1/6 / 5/6, total 60000.
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
    assert by_qty[1]["market_value"] == pytest.approx(1 * 100.0 * 100.0)  # 10000, NOT 50000
    assert by_qty[5]["market_value"] == pytest.approx(5 * 100.0 * 100.0)  # 50000
    assert body["totals"]["market_value"] == pytest.approx(60000.0)
    assert by_qty[1]["weight"] == pytest.approx(10000.0 / 60000.0)
    assert by_qty[5]["weight"] == pytest.approx(50000.0 / 60000.0)


def test_options_sleeve_distinct_reason_when_option_underlier_not_in_cache(store):
    # Fix-round-1 MINOR: an OPT position whose underlier has no symbol-map
    # entry (or no cached bars) used to fall through to the generic "chain
    # not ingested" reason — it must name the real problem instead.
    portfolio = Portfolio(
        positions=(Position(con_id=555, symbol="NOPE", qty=1, sec_type="OPT", multiplier=100.0),),
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


# --- Never-500 guards: broker death mid-session + account summary hygiene ---


class DeadBroker:
    """Gateway died mid-session: every broker call raises (batch-1 final
    review F3 — the flagship page must degrade to an honest empty book +
    note, never a 500)."""

    async def get_portfolio(self) -> Portfolio:
        raise ConnectionError("gateway dropped")

    async def get_account_summary(self):
        raise ConnectionError("gateway dropped")


def test_broker_get_portfolio_failure_degrades_to_empty_book_not_500(store):
    client = _client(store, broker=DeadBroker())
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["totals"]["n_positions"] == 0
    assert body["account"] is None
    assert body["account_note"]
    assert "failed" in body["account_note"]


def test_account_summary_nan_value_serializes_as_null_not_nan_token(store):
    # F6: AccountOut floats bypassed clean() — a NaN from the broker
    # serialized as a bare NaN token (invalid JSON). parse_constant trips
    # if any non-finite token sneaks into the body.
    summary = {
        "net_liquidation": float("nan"),
        "total_cash_value": 20000.0,
        "gross_position_value": 105000.5,
        "buying_power": 60000.0,
    }
    broker = FakeBroker(Portfolio(positions=(), as_of="2026-07-24"), account_summary=summary)
    r = _client(store, broker=broker).get("/api/portfolio")
    assert r.status_code == 200
    body = json.loads(
        r.text, parse_constant=lambda tok: pytest.fail(f"non-finite JSON token {tok!r} in body")
    )
    assert body["account"]["net_liquidation"] is None
    assert body["account"]["total_cash_value"] == pytest.approx(20000.0)


def test_account_summary_missing_key_degrades_to_null_account_not_500(store):
    # F6: AccountOut(**summary) was built OUTSIDE the try — a partial dict
    # raised ValidationError -> 500. It must degrade to a null account + note.
    broker = FakeBroker(
        Portfolio(positions=(), as_of="2026-07-24"),
        account_summary={"net_liquidation": 125000.5},
    )
    r = _client(store, broker=broker).get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["account"] is None
    assert body["account_note"]
