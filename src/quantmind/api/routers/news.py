"""News domain routes (wave-3B Today task): GET /api/news feeds the Regime
box's scrolling ticker.

Unlike most GET routers (macro.py, instruments.py), this one reads the LIVE
broker (pattern: portfolio.py's GET /api/portfolio) rather than the cache —
news has no meaningful "cached" form, an hour-old headline list is stale in a
way an hour-old close price isn't. `broker=None` (no Gateway connection) is a
structured, honest empty response, never a 500 (Global Constraints).

Coordination note (does NOT touch ib_broker.py, which is A2's file this
wave): `app.state.broker` is always an `IbBroker` instance in production
(api/main.py), which stores its raw `ib_async.IB` client on a private `_ib`
attribute. Rather than add a public news method to `IbBroker` (out of scope
for this task's file ownership) or edit api/main.py/app.py (also unowned),
this router reaches into that attribute via `getattr(broker, "_ib", None)` —
a deliberate, narrow coupling to `IbBroker`'s current shape, documented here
so a future refactor of ib_broker.py knows to keep `_ib` (or update this
one call site). Any broker double lacking `_ib` (or `None`) degrades to the
same honest-empty path a real Gateway-down account gets.

Filtering: IBKR's broadtape (general market) news is noisy — GET /api/news
keeps only headlines that either name a symbol from the cached universe
(`store.read_symbol_map()`) or contain a macro keyword (Fed, CPI, yields,
...), capped at 50, newest first.
"""

from __future__ import annotations

import re

import pandas as pd
from fastapi import APIRouter, Request
from pydantic import BaseModel

from quantmind.broker.ib_news import IbNews, NewsHeadline

router = APIRouter()

_LOOKBACK_HOURS = 48
_MAX_FETCH = 100
_MAX_ITEMS = 50

# Honest-empty notes, one per distinguishable cause (fix-round-1: the live
# paper session returned empty with a single ambiguous message — the user
# couldn't tell whether to start the Gateway, fix entitlements, or shrug):
_NO_GATEWAY_NOTE = "Gateway not connected"
_NO_PROVIDERS_NOTE = (
    "no entitled news providers on this session — check paper-account data sharing"
)
_NO_RELEVANT_NOTE = "no relevant headlines"
_NO_RELEVANT_FALLBACK_NOTE = "no book-relevant headlines — showing latest broadtape"
_REQUEST_FAILED_NOTE = "news request failed — Gateway connection error"

# Macro keywords (lowercase, matched as substrings of the lowercased
# headline) — deliberately broad; a false-positive macro tag just means one
# extra ticker item, a missed one means a Fed/CPI headline silently drops.
MACRO_KEYWORDS = [
    "fed", "fomc", "cpi", "inflation", "jobs report", "nonfarm payrolls",
    "yields", "yield curve", "rate cut", "rate hike", "interest rate",
    "gdp", "unemployment", "ecb", "boj", "ism", "pce", "treasury",
    "recession", "tariff", "powell", "jobless claims",
]

_WORD_RE = re.compile(r"[A-Za-z]+")


class NewsItemOut(BaseModel):
    time: str
    source: str
    headline: str
    symbol: str | None = None
    url: str | None = None


class NewsResponse(BaseModel):
    items: list[NewsItemOut]
    as_of: str | None
    note: str | None


def _iso(dt) -> str:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _matched_symbol(headline: str, symbols: set[str]) -> str | None:
    words = set(_WORD_RE.findall(headline.upper()))
    for symbol in symbols:
        if symbol.upper() in words:
            return symbol
    return None


def _is_macro(headline: str) -> bool:
    low = headline.lower()
    return any(keyword in low for keyword in MACRO_KEYWORDS)


def _to_item(h: NewsHeadline, symbols: set[str]) -> NewsItemOut | None:
    symbol = _matched_symbol(h.headline, symbols)
    if symbol is None and not _is_macro(h.headline):
        return None
    return NewsItemOut(time=_iso(h.time), source=h.source, headline=h.headline, symbol=symbol, url=h.url)


def _empty(note: str) -> NewsResponse:
    return NewsResponse(items=[], as_of=None, note=note)


@router.get("/news", response_model=NewsResponse)
async def get_news(request: Request) -> NewsResponse:
    store = request.app.state.store
    broker = request.app.state.broker
    ib = getattr(broker, "_ib", None) if broker is not None else None
    if ib is None:
        return _empty(_NO_GATEWAY_NOTE)

    news_client = IbNews(ib)
    try:
        # Providers fetched separately from headlines so the empty states
        # stay distinguishable: zero entitlements is an account/session
        # problem, zero relevant headlines is just a quiet tape.
        providers = await news_client.provider_codes()
        if not providers:
            return _empty(_NO_PROVIDERS_NOTE)
        headlines = await news_client.broadtape_headlines(
            lookback_hours=_LOOKBACK_HOURS, max_results=_MAX_FETCH, providers=providers
        )
    except Exception:
        # Never-500 law: any network/IB-side failure (timeout, disconnect,
        # malformed response) degrades to an honest empty state.
        return _empty(_REQUEST_FAILED_NOTE)

    symbols = set(store.read_symbol_map().keys())
    items = [item for item in (_to_item(h, symbols) for h in headlines) if item is not None]
    note = None
    if not items:
        if not headlines:
            # Entitled providers exist but the feed returned nothing at all
            # in the window — quiet tape, not a broken pipe.
            return _empty(_NO_RELEVANT_NOTE)
        # The relevance filter passed zero items but the tape ISN'T empty
        # (batch-1 final review adjudication a): an empty ticker reads as
        # broken, so fall back to the latest broadtape — same cap/ordering
        # as the filtered path — and say so honestly.
        items = [
            NewsItemOut(
                time=_iso(h.time), source=h.source, headline=h.headline,
                symbol=_matched_symbol(h.headline, symbols), url=h.url,
            )
            for h in headlines
        ]
        note = _NO_RELEVANT_FALLBACK_NOTE

    items.sort(key=lambda i: i.time, reverse=True)
    items = items[:_MAX_ITEMS]
    return NewsResponse(items=items, as_of=items[0].time, note=note)
