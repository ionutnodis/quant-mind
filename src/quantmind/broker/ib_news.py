"""Thin ib_async wrapper for IBKR news (wave-3B Task: Today overhaul).

Mirrors ib_broker.py's shape (Engineering Constraint 3's "I/O only in
broker/"): mapping/filtering logic is pure and unit-tested against fakes
(tests/test_ib_news.py, pattern: tests/test_ib_broker_mapping.py's FakeIB),
the network-touching calls (`reqNewsProvidersAsync`, `reqHistoricalNewsAsync`)
are exercised only against that fake here — there's no opt-in E2E smoke test
for news yet (unlike ib_broker.py's bars), since a Gateway paper account may
not carry any news entitlements at all; the honest-empty path (no providers
-> `[]`) is what routers/news.py leans on when that's the case.

`reqHistoricalNewsAsync` needs a `conId` — IBKR's documented convention for
"general/broadtape" news (not tied to one instrument) is `conId=0`, which is
what `broadtape_headlines` uses; this repo doesn't attempt per-symbol news
fan-out (would be one paced call per cached symbol, expensive and easy to
rate-limit) and instead lets routers/news.py filter the single broadtape pull
down to cached-universe symbols + macro keywords by matching headline text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class NewsHeadline:
    time: datetime
    source: str
    headline: str
    article_id: str
    url: str | None = None


class IbNews:
    def __init__(self, ib):
        self._ib = ib

    async def provider_codes(self) -> list[str]:
        """Entitled news providers for this account. Empty list (no
        entitlements, or Gateway not connected to a news feed) is a normal,
        expected result — never an error."""
        providers = await self._ib.reqNewsProvidersAsync()
        return [p.code for p in providers if p.code]

    async def broadtape_headlines(
        self,
        lookback_hours: int = 48,
        max_results: int = 100,
        providers: list[str] | None = None,
    ) -> list[NewsHeadline]:
        """General-market headlines (conId=0) from every entitled provider
        over the trailing `lookback_hours`. `[]` when there are no entitled
        providers or IBKR returns nothing (Gateway down, feed empty, etc.) —
        the caller (routers/news.py) turns that into an honest,
        case-specific empty state, never a crash. Pass `providers` (from an
        earlier `provider_codes()` call) to skip re-fetching the provider
        list — routers/news.py needs it separately anyway, to distinguish
        "no entitlements" from "entitled but nothing relevant"."""
        if providers is None:
            providers = await self.provider_codes()
        if not providers:
            return []
        provider_codes = "+".join(providers)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        result = await self._ib.reqHistoricalNewsAsync(
            0, provider_codes, start, end, max_results
        )
        if not result:
            return []
        return [
            NewsHeadline(
                time=h.time,
                source=h.providerCode,
                headline=h.headline,
                article_id=h.articleId,
            )
            for h in result
        ]
