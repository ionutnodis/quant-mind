"""API contract tests for the instruments domain (Task A2): metadata + derived
stats (52w range, ann vol, beta vs benchmark) and the OHLC candle window.
Serialization policy: UTC ISO timestamps, NaN -> null, unknown symbol -> 422,
never a 500 (pattern: tests/test_api_risk.py)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantmind.datastore.store import BarMeta, BarStore
from quantmind.fx import EcbFxProvider, sync_ecb_fx
from quantmind.instruments.metadata import (
    DistributionPolicy,
    MetadataProvenanceV1,
    UcitsEtfProfileV1,
)

# Load instruments.py directly from its file rather than via
# `quantmind.api.routers.instruments` (which forces Python to first run
# `quantmind/api/routers/__init__.py`, eagerly importing every sibling-owned
# router — several of which are mid-edit in this shared wave-3 tree at any
# given moment). instruments.py is this task's exclusive file and only
# imports from quantmind.risk (an unrelated, stable package), so this keeps
# these tests deterministic regardless of sibling task state.
_INSTRUMENTS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src"
    / "quantmind"
    / "api"
    / "routers"
    / "instruments.py"
)
_spec = importlib.util.spec_from_file_location("_instruments_under_test", _INSTRUMENTS_PATH)
_instruments_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _instruments_module  # pydantic resolves forward refs via sys.modules
_spec.loader.exec_module(_instruments_module)
instruments_router = _instruments_module.router


def _make_app(
    store: BarStore, benchmark: str = "SPY", base_currency: str = "USD"
) -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.state.benchmark = benchmark
    app.state.base_currency = base_currency
    app.include_router(instruments_router, prefix="/api")
    return app


def _bars(n=300, seed=1, price0=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = price0 * np.abs(np.cumprod(1 + rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0},
        index=idx,
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars(seed=2, price0=50.0), meta=meta)
    store.write_symbol_map({"SPY": 1, "EEM": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata(
        "EEM",
        {
            "con_id": 2,
            "long_name": "iShares MSCI Emerging Markets ETF",
            "exchange": "ARCA",
            "currency": "USD",
            "sec_type": "STK",
            "industry": None,
            "region": "Emerging Markets",
            "provider": "ibkr",
        },
    )
    app = _make_app(store, benchmark="SPY")
    return TestClient(app, base_url="http://127.0.0.1")


def test_instrument_returns_metadata_and_derived_stats(client):
    r = client.get("/api/instruments/EEM")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "EEM"
    assert body["con_id"] == 2
    assert body["long_name"] == "iShares MSCI Emerging Markets ETF"
    assert body["exchange"] == "ARCA"
    assert body["region"] == "Emerging Markets"
    assert body["provider"] == "ibkr"
    assert body["last_close"] is not None
    assert body["high_52w"] is not None
    assert body["low_52w"] is not None
    # high/low are max/min over the trailing 52w window, which includes last_close.
    assert body["high_52w"] >= body["last_close"] >= body["low_52w"]
    assert body["ann_vol"] is None or body["ann_vol"] >= 0
    assert body["beta_benchmark"] == "SPY"
    assert body["risk_base_currency"] == "USD"
    assert body["as_of"] is not None


def test_instrument_missing_metadata_returns_nulls_not_crash(client):
    r = client.get("/api/instruments/SPY")
    assert r.status_code == 200
    body = r.json()
    assert body["long_name"] is None
    assert body["region"] is None
    # self-beta vs itself is exactly 1.0
    assert body["beta"] == pytest.approx(1.0)
    assert body["beta_benchmark"] == "SPY"


def test_instrument_beta_matches_risk_after_dated_fx_normalization(tmp_path):
    from quantmind.api.app import create_app

    store = BarStore(tmp_path)
    index = pd.bdate_range(end="2026-07-24", periods=300)
    rng = np.random.default_rng(41)
    benchmark_close = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(index)))
    fx = np.linspace(1.05, 1.25, len(index))
    local_eur_close = benchmark_close / fx

    def frame(close):
        return pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000.0,
            },
            index=index,
        )

    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", frame(benchmark_close), meta)
    store.write_bars(2, "1d", frame(local_eur_close), meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("IWDA", {"con_id": 2, "currency": "EUR"})
    rows = ["CURRENCY,TIME_PERIOD,OBS_VALUE"]
    rows.extend(
        f"USD,{timestamp.date().isoformat()},{rate:.10f}"
        for timestamp, rate in zip(index, fx)
    )
    sync_ecb_fx(
        store,
        EcbFxProvider(fetcher=lambda _url: "\n".join(rows)),
        {"USD", "EUR"},
        today=index[-1].date(),
        fetched_at="2026-07-24T17:00:00Z",
    )
    client = TestClient(
        create_app(store=store, benchmark="SPY", base_currency="USD"),
        base_url="http://127.0.0.1",
    )

    instrument = client.get("/api/instruments/IWDA").json()
    risk = client.get("/api/risk/IWDA", params={"window": 60, "years": 5}).json()
    risk_beta = [point["beta"] for point in risk["beta_series"] if point["beta"] is not None][-1]

    assert instrument["beta"] == pytest.approx(risk_beta)
    assert instrument["beta"] == pytest.approx(1.0, abs=1e-7)
    assert instrument["risk_base_currency"] == "USD"
    assert instrument["risk_fx_source"] == "ECB"
    assert instrument["risk_fx_as_of"] == "2026-07-24"
    assert instrument["risk"] == {
        "status": "ready",
        "reason": None,
        "benchmark": "SPY",
        "base_currency": "USD",
        "fx": {
            "status": "converted",
            "base_currency": "USD",
            "source": "ECB",
            "as_of": "2026-07-24",
            "fetched_at": "2026-07-24T17:00:00Z",
            "missing_currencies": [],
            "note": "Prices are normalized to USD with dated ECB evidence.",
        },
        "note": "Volatility and beta are ready from USD-normalized history.",
    }


def test_instrument_risk_contract_names_fx_failure_without_hiding_metadata(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", _bars(seed=1), meta)
    store.write_bars(2, "1d", _bars(seed=2), meta)
    store.write_symbol_map({"SPY": 1, "IWDA": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 2,
            "currency": "EUR",
            "long_name": "iShares Core MSCI World",
        },
    )

    body = TestClient(_make_app(store), base_url="http://127.0.0.1").get(
        "/api/instruments/IWDA"
    ).json()

    assert body["long_name"] == "iShares Core MSCI World"
    assert body["last_close"] is not None
    assert body["ann_vol"] is None
    assert body["beta"] is None
    assert body["risk"]["status"] == "unavailable"
    assert body["risk"]["reason"] == "fx_unavailable"
    assert body["risk"]["fx"]["status"] == "incomplete"
    assert body["risk"]["fx"]["missing_currencies"] == ["EUR"]


def test_instrument_risk_contract_keeps_vol_when_benchmark_is_missing(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(
        2,
        "1d",
        _bars(seed=2),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"EEM": 2})
    store.write_instrument_metadata("EEM", {"con_id": 2, "currency": "USD"})

    body = TestClient(
        _make_app(store, benchmark="SPY"), base_url="http://127.0.0.1"
    ).get("/api/instruments/EEM").json()

    assert body["ann_vol"] is not None
    assert body["beta"] is None
    assert body["risk"]["status"] == "partial"
    assert body["risk"]["reason"] == "missing_benchmark"
    assert body["risk"]["fx"]["status"] == "identity"


def test_instrument_keeps_vol_when_foreign_benchmark_fx_is_missing(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", _bars(seed=1), meta)
    store.write_bars(2, "1d", _bars(seed=2), meta)
    store.write_symbol_map({"LOCAL": 1, "EU_BENCH": 2})
    store.write_instrument_metadata("LOCAL", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata(
        "EU_BENCH", {"con_id": 2, "currency": "EUR"}
    )

    body = TestClient(
        _make_app(store, benchmark="EU_BENCH"), base_url="http://127.0.0.1"
    ).get("/api/instruments/LOCAL").json()

    assert body["ann_vol"] is not None
    assert body["beta"] is None
    assert body["risk"]["status"] == "partial"
    assert body["risk"]["reason"] == "fx_unavailable"
    assert body["risk"]["fx"]["missing_currencies"] == ["EUR"]
    assert "beta vs EU_BENCH is unavailable" in body["risk"]["note"]


def test_instrument_risk_contract_names_insufficient_history(tmp_path):
    store = BarStore(tmp_path)
    short_history = _bars(n=6, seed=5)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", short_history, meta)
    store.write_bars(2, "1d", short_history.copy(), meta)
    store.write_symbol_map({"SPY": 1, "CLONE": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("CLONE", {"con_id": 2, "currency": "USD"})

    body = TestClient(_make_app(store), base_url="http://127.0.0.1").get(
        "/api/instruments/CLONE"
    ).json()

    assert body["ann_vol"] is not None
    assert body["beta"] is None
    assert body["risk"]["status"] == "partial"
    assert body["risk"]["reason"] == "insufficient_history"
    assert body["risk"]["fx"]["status"] == "identity"


def test_instrument_exposes_ucits_listing_identity_and_profile(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(
        7,
        "1d",
        _bars(seed=7),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 7})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 7,
            "provider": "ibkr",
            "currency": "EUR",
            "exchange": "SMART",
            "primary_exchange": "AEB",
            "local_symbol": "IWDA",
            "trading_class": "IWDA",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
            "valid_exchanges": ["SMART", "AEB"],
            "issuer_id": "issuer-1",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "FRESH",
            "ucits_profile_reason": None,
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
            replication_method="Physical · Optimized sampling",
            benchmark_name="MSCI World",
            provenance=MetadataProvenanceV1(
                source="justetf",
                source_url="https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
                fetched_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
            ),
        )
    )
    response = TestClient(_make_app(store), base_url="http://127.0.0.1").get(
        "/api/instruments/IWDA"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["isin"] == "IE00B4L5Y983"
    assert body["primary_exchange"] == "AEB"
    assert body["stock_type"] == "ETF"
    assert body["ucits_profile_status"] == "FRESH"
    assert body["ucits_profile"]["ter_pct"] == "0.20"
    assert body["ucits_profile"]["benchmark_name"] == "MSCI World"
    assert body["ucits_profile"]["provenance"]["source"] == "justetf"


def test_instrument_withholds_a_profile_after_its_freshness_window(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(
        7,
        "1d",
        _bars(seed=7),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 7})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 7,
            "currency": "EUR",
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
                fetched_at_utc=datetime.now(UTC) - timedelta(days=31),
            ),
        )
    )

    body = TestClient(_make_app(store), base_url="http://127.0.0.1").get(
        "/api/instruments/IWDA"
    ).json()

    assert body["ucits_profile_status"] == "STALE"
    assert body["ucits_profile"] is None
    assert "30" in body["ucits_profile_reason"]


def test_instrument_exposes_last_successful_provenance_without_stale_profile_facts(
    tmp_path,
):
    store = BarStore(tmp_path)
    store.write_bars(
        7,
        "1d",
        _bars(seed=7),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 7})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 7,
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "STALE",
            "ucits_profile_reason": "justETF refresh failed (TimeoutError)",
            "ucits_profile_last_successful_provenance": {
                "source": "justetf",
                "source_url": "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
                "fetched_at_utc": "2026-08-01T12:00:00Z",
            },
        },
    )

    body = TestClient(
        _make_app(store, benchmark="IWDA", base_currency="EUR"),
        base_url="http://127.0.0.1",
    ).get("/api/instruments/IWDA").json()

    assert body["ucits_profile_status"] == "STALE"
    assert body["ucits_profile"] is None
    assert body["ucits_profile_last_successful_provenance"] == {
        "source": "justetf",
        "source_url": "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
        "fetched_at_utc": "2026-08-01T12:00:00Z",
    }


def test_instrument_reclassifies_a_missing_fresh_profile_file(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(
        7,
        "1d",
        _bars(seed=7),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 7})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 7,
            "currency": "EUR",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "FRESH",
        },
    )

    body = TestClient(
        _make_app(store, benchmark="IWDA", base_currency="EUR"),
        base_url="http://127.0.0.1",
    ).get("/api/instruments/IWDA").json()

    assert body["ucits_profile_status"] == "MISSING"
    assert body["ucits_profile"] is None
    assert "missing" in body["ucits_profile_reason"]


def test_instrument_downgrades_corrupt_fresh_ucits_cache_without_a_500(tmp_path):
    store = BarStore(tmp_path)
    store.write_bars(
        7,
        "1d",
        _bars(seed=7),
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"IWDA": 7})
    store.write_instrument_metadata(
        "IWDA",
        {
            "con_id": 7,
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
            "ucits_profile_isin": "IE00B4L5Y983",
            "ucits_profile_status": "FRESH",
        },
    )
    profile_dir = tmp_path / "ucits_profiles"
    profile_dir.mkdir()
    (profile_dir / "IE00B4L5Y983.json").write_text("not-json")

    response = TestClient(_make_app(store), base_url="http://127.0.0.1").get(
        "/api/instruments/IWDA"
    )

    assert response.status_code == 200
    assert response.json()["ucits_profile"] is None
    assert response.json()["ucits_profile_status"] == "MISSING"
    assert "corrupt" in response.json()["ucits_profile_reason"]


def test_instrument_rejects_metadata_bound_to_a_different_contract(tmp_path):
    store = BarStore(tmp_path)
    bars = _bars(seed=5)
    store.write_bars(
        2,
        "1d",
        bars,
        BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24"),
    )
    store.write_symbol_map({"ASML": 2})
    store.write_instrument_metadata(
        "ASML",
        {
            "con_id": 1,
            "currency": "USD",
            "exchange": "NASDAQ",
            "long_name": "OLD LISTING",
        },
    )
    client = TestClient(_make_app(store, benchmark="ASML"))

    response = client.get("/api/instruments/ASML")

    assert response.status_code == 422
    assert "metadata contract identity" in response.json()["detail"]
    assert "ASML" in response.json()["detail"]


def test_instrument_unknown_symbol_is_422_not_500(client):
    r = client.get("/api/instruments/NOPE")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_instrument_corrupt_metadata_is_a_named_422_not_a_500(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", _bars(seed=1), meta)
    store.write_symbol_map({"SPY": 1})
    (tmp_path / "instruments.json").write_text('{"SPY": 7}')
    client = TestClient(_make_app(store), base_url="http://127.0.0.1")

    response = client.get("/api/instruments/SPY")

    assert response.status_code == 422
    assert "instrument metadata cache is corrupt" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        '{"SPY": {"currency": "USD", "valid_exchanges": 7}}',
        '{"SPY": {"currency": "USD", "ucits_profile_status": "READY"}}',
    ],
)
def test_instrument_invalid_metadata_fields_are_named_422_not_response_500(
    tmp_path, payload
):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(1, "1d", _bars(seed=1), meta)
    store.write_symbol_map({"SPY": 1})
    (tmp_path / "instruments.json").write_text(payload)
    client = TestClient(
        _make_app(store),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    )

    response = client.get("/api/instruments/SPY")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "instrument metadata cache is corrupt; run sync to rebuild it"
    )


def test_instrument_mapped_but_barless_symbol_is_422(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    store.write_symbol_map({"SPY": 1, "GHOST": 99})
    app = _make_app(store, benchmark="SPY")
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/instruments/GHOST")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_candles_returns_ohlc_window_bounded_by_days(client):
    r = client.get("/api/instruments/SPY/candles", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert body["days"] == 30
    assert len(body["candles"]) == 30
    c = body["candles"][0]
    assert set(c.keys()) == {"date", "open", "high", "low", "close", "volume"}
    assert c["close"] is not None


def test_candles_default_days_and_unknown_symbol_422(client):
    r = client.get("/api/instruments/SPY/candles")
    assert r.status_code == 200
    assert r.json()["days"] == 180

    r2 = client.get("/api/instruments/NOPE/candles")
    assert r2.status_code == 422


def test_candles_bounds_reject_out_of_range(client):
    r = client.get("/api/instruments/SPY/candles", params={"days": 0})
    assert r.status_code == 422
    r2 = client.get("/api/instruments/SPY/candles", params={"days": 100_000})
    assert r2.status_code == 422


def test_instrument_beta_of_benchmark_series_correlates_when_identical(tmp_path):
    # A symbol whose returns exactly track the benchmark should show beta ~ 1.
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    bars = _bars(seed=5)
    store.write_bars(con_id=1, bar_size="1d", bars=bars, meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=bars.copy(), meta=meta)  # identical series
    store.write_symbol_map({"SPY": 1, "CLONE": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("CLONE", {"con_id": 2, "currency": "USD"})
    app = _make_app(store, benchmark="SPY")
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/instruments/CLONE")
    assert r.status_code == 200
    assert r.json()["beta"] == pytest.approx(1.0, abs=1e-6)
