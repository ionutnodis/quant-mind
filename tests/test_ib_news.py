"""ib_news.py mapping/filtering logic against a fake ib_async IB stub — no
network (pattern: tests/test_ib_broker_mapping.py's FakeIB), using real
ib_async dataclasses (NewsProvider, HistoricalNews) so the mapping is exactly
what production sees.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ib_async import HistoricalNews, NewsProvider

from quantmind.broker.ib_news import IbNews, NewsHeadline


class FakeIB:
    def __init__(self, providers=None, historical_news=None):
        self._providers = providers if providers is not None else []
        self._historical_news = historical_news if historical_news is not None else []
        self.reqNewsProviders_calls = 0
        self.reqHistoricalNews_calls = []

    async def reqNewsProvidersAsync(self):
        self.reqNewsProviders_calls += 1
        return self._providers

    async def reqHistoricalNewsAsync(self, conId, providerCodes, startDateTime, endDateTime, totalResults):
        self.reqHistoricalNews_calls.append(
            (conId, providerCodes, startDateTime, endDateTime, totalResults)
        )
        return self._historical_news


def _hn(headline: str, provider="BRFG", article_id="a1", when=None):
    return HistoricalNews(
        when or datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc), provider, article_id, headline
    )


async def test_provider_codes_maps_and_drops_blank_codes():
    ib = FakeIB(providers=[NewsProvider(code="BRFG", name="Briefing.com"), NewsProvider(code="", name="empty")])
    news = IbNews(ib)
    codes = await news.provider_codes()
    assert codes == ["BRFG"]


async def test_provider_codes_empty_when_no_entitlements():
    ib = FakeIB(providers=[])
    news = IbNews(ib)
    assert await news.provider_codes() == []


async def test_broadtape_headlines_returns_empty_without_calling_historical_news_when_no_providers():
    ib = FakeIB(providers=[])
    news = IbNews(ib)
    result = await news.broadtape_headlines()
    assert result == []
    assert ib.reqHistoricalNews_calls == []


async def test_broadtape_headlines_maps_fields_and_uses_conid_zero():
    ib = FakeIB(
        providers=[NewsProvider(code="BRFG", name="Briefing.com"), NewsProvider(code="DJNL", name="Dow Jones")],
        historical_news=[_hn("Fed holds rates steady", provider="BRFG", article_id="a1")],
    )
    news = IbNews(ib)
    result = await news.broadtape_headlines(lookback_hours=24, max_results=50)
    assert result == [
        NewsHeadline(
            time=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
            source="BRFG",
            headline="Fed holds rates steady",
            article_id="a1",
        )
    ]
    conId, providerCodes, start, end, totalResults = ib.reqHistoricalNews_calls[0]
    assert conId == 0
    assert providerCodes == "BRFG+DJNL"
    assert totalResults == 50
    assert (end - start).total_seconds() == pytest.approx(24 * 3600, abs=5)


async def test_broadtape_headlines_returns_empty_when_ib_returns_none():
    ib = FakeIB(providers=[NewsProvider(code="BRFG", name="Briefing.com")], historical_news=None)
    news = IbNews(ib)
    assert await news.broadtape_headlines() == []


async def test_broadtape_headlines_preserves_multiple_articles():
    ib = FakeIB(
        providers=[NewsProvider(code="BRFG", name="Briefing.com")],
        historical_news=[_hn("Headline one", article_id="a1"), _hn("Headline two", article_id="a2")],
    )
    news = IbNews(ib)
    result = await news.broadtape_headlines()
    assert [h.article_id for h in result] == ["a1", "a2"]
