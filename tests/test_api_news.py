"""API contract tests for GET /api/news: reads the live broker's news feed
(pattern: routers/portfolio.py's live GET /api/portfolio), filters to
cached-universe symbols + macro keywords, never a 500.

`app.state.broker` is faked as an object exposing `_ib` (the shape
routers/news.py's `getattr(broker, "_ib", None)` looks for — mirrors
`IbBroker`'s real attribute, see news.py's module docstring for why).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from ib_async import HistoricalNews, NewsProvider

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore


class FakeIb:
    def __init__(
        self,
        providers=None,
        historical_news=None,
        raise_on_history=False,
        raise_on_providers=False,
    ):
        self._providers = providers if providers is not None else []
        self._historical_news = historical_news if historical_news is not None else []
        self._raise_on_history = raise_on_history
        self._raise_on_providers = raise_on_providers

    async def reqNewsProvidersAsync(self):
        if self._raise_on_providers:
            raise ConnectionError("gateway dropped")
        return self._providers

    async def reqHistoricalNewsAsync(self, conId, providerCodes, startDateTime, endDateTime, totalResults):
        if self._raise_on_history:
            raise ConnectionError("gateway dropped")
        return self._historical_news


class FakeBroker:
    def __init__(self, ib):
        self._ib = ib


def _hn(headline: str, provider="BRFG", article_id="a1", when=None):
    return HistoricalNews(
        when or datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc), provider, article_id, headline
    )


@pytest.fixture
def store(tmp_path) -> BarStore:
    s = BarStore(tmp_path)
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    close = pd.Series(100.0, index=idx)
    bars = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1000.0})
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    s.write_bars(con_id=1, bar_size="1d", bars=bars, meta=meta)
    s.write_symbol_map({"SPY": 1})
    return s


def _client(store: BarStore, broker=None) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", broker=broker, base_currency="USD")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


# The three empty causes get three DISTINCT notes (fix-round-1: a live
# paper session couldn't tell "start the Gateway" from "fix entitlements"
# from "quiet tape" behind one ambiguous message).


def test_no_broker_says_gateway_not_connected(store):
    r = _client(store, broker=None).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["as_of"] is None
    assert body["note"] == "Gateway not connected"


def test_broker_with_no_providers_says_no_entitled_providers(store):
    broker = FakeBroker(FakeIb(providers=[]))
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "no entitled news providers" in body["note"]
    assert "data sharing" in body["note"]


def test_providers_but_zero_headlines_says_no_relevant_headlines(store):
    broker = FakeBroker(FakeIb(providers=[NewsProvider(code="BRFG", name="x")], historical_news=[]))
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["note"] == "no relevant headlines"


def test_history_error_never_500_says_request_failed(store):
    broker = FakeBroker(FakeIb(providers=[NewsProvider(code="BRFG", name="x")], raise_on_history=True))
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "news request failed" in body["note"]


def test_provider_fetch_error_never_500_says_request_failed(store):
    broker = FakeBroker(FakeIb(raise_on_providers=True))
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "news request failed" in body["note"]


def test_filters_to_universe_symbol_and_macro_keyword_drops_irrelevant(store):
    broker = FakeBroker(
        FakeIb(
            providers=[NewsProvider(code="BRFG", name="x")],
            historical_news=[
                _hn("SPY rallies into the close", article_id="a1", when=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)),
                _hn("Fed holds rates steady", article_id="a2", when=datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc)),
                _hn("Local bakery wins a pastry award", article_id="a3"),
            ],
        )
    )
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    headlines = {item["headline"] for item in body["items"]}
    assert headlines == {"SPY rallies into the close", "Fed holds rates steady"}
    assert body["note"] is None
    assert body["as_of"] is not None and body["as_of"].endswith("Z")

    spy_item = next(i for i in body["items"] if i["headline"].startswith("SPY"))
    assert spy_item["symbol"] == "SPY"
    fed_item = next(i for i in body["items"] if i["headline"].startswith("Fed"))
    assert fed_item["symbol"] is None


def test_sorted_newest_first_and_capped_at_50(store):
    items = [
        _hn(f"Fed headline {i}", article_id=f"a{i}", when=datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc) + pd.Timedelta(minutes=i))
        for i in range(60)
    ]
    broker = FakeBroker(FakeIb(providers=[NewsProvider(code="BRFG", name="x")], historical_news=items))
    r = _client(store, broker=broker).get("/api/news")
    body = r.json()
    assert len(body["items"]) == 50
    assert body["items"][0]["headline"] == "Fed headline 59"


def test_irrelevant_only_headlines_fall_back_to_latest_broadtape(store):
    # Batch-1 final review adjudication (a): when the relevance filter passes
    # zero items but the raw tape isn't empty, an empty ticker reads as
    # broken — show the latest broadtape and say so honestly instead.
    broker = FakeBroker(
        FakeIb(
            providers=[NewsProvider(code="BRFG", name="x")],
            historical_news=[_hn("Local bakery wins a pastry award")],
        )
    )
    r = _client(store, broker=broker).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["headline"] == "Local bakery wins a pastry award"
    assert body["note"] == "no book-relevant headlines — showing latest broadtape"
    assert body["as_of"] == body["items"][0]["time"]


def test_broadtape_fallback_sorted_newest_first_and_capped_at_50(store):
    # The fallback path honors the same cap/ordering as the filtered path.
    items = [
        _hn(f"Pastry award {i}", article_id=f"b{i}", when=datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc) + pd.Timedelta(minutes=i))
        for i in range(60)
    ]
    broker = FakeBroker(FakeIb(providers=[NewsProvider(code="BRFG", name="x")], historical_news=items))
    r = _client(store, broker=broker).get("/api/news")
    body = r.json()
    assert len(body["items"]) == 50
    assert body["items"][0]["headline"] == "Pastry award 59"
    assert "showing latest broadtape" in body["note"]
