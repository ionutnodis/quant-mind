"""Deterministic attention ranking, deliberately not risk attribution.

Only direct symbol/company mentions create holding matches. Topic/region
interests have independent explanations; owning an ETF never implies an
unverified holding in its constituents. Quantity/sign do not imply impact.
"""
from __future__ import annotations

from datetime import datetime
import re

from pydantic import Field

from quantmind.world.models import WorldEvent, WorldProfile

# Small, explicit company-name dictionary, not a fuzzy security master. Unknown
# instruments still work via exact cashtag/ticker matching. Multiple listings
# are not silently collapsed (ASML.AS remains distinct from ASML).
COMPANY_NAMES = {
    "NVDA": ("nvidia",), "AMD": ("advanced micro devices",),
    "MU": ("micron",), "AVGO": ("broadcom",),
    "TSM": ("taiwan semiconductor", "tsmc"),
    "ASML": ("asml",), "ASML.AS": ("asml",),
    "MSFT": ("microsoft",), "GOOG": ("alphabet", "google"),
    "GOOGL": ("alphabet", "google"), "META": ("meta platforms", "facebook"),
    "AMZN": ("amazon",), "AAPL": ("apple inc", "apple shares", "apple stock"),
    "ORCL": ("oracle",), "PLTR": ("palantir",), "SMCI": ("super micro computer",),
    "VRT": ("vertiv",), "CEG": ("constellation energy",),
    "GEV": ("ge vernova",), "ARM": ("arm holdings",),
    "AI": ("c3.ai",), "IT": ("gartner",), "ON": ("on semiconductor", "onsemi"),
    "VOD.L": ("vodafone",), "SAP.DE": ("sap se",),
    "SIE.DE": ("siemens",), "NESN.SW": ("nestle", "nestlé"),
}
AMBIGUOUS = {"META", "ARM", "COST", "LIFE", "OPEN", "LOVE", "TRUE", "GOOD", "WORK", "SAFE"}
TOPIC_WORDS = {
    "semiconductors": ("semiconductor", "semiconductors", "chip", "chips", "memory", "foundry"),
    "ai": ("ai", "artificial intelligence", "data center", "data centre"),
    "energy": ("energy", "oil", "natural gas", "electricity", "power grid", "nuclear"),
    "rates": ("rates", "interest rate", "monetary policy", "central bank", "yield"),
    "inflation": ("inflation", "consumer prices", "cpi", "ppi"),
    "geopolitics": ("geopolitics", "sanctions", "tariff", "tariffs", "conflict", "ceasefire"),
    "supply chain": ("supply chain", "shipping", "port", "chokepoint", "earthquake"),
}
REGION_WORDS = {
    "europe": ("europe", "european", "eurozone", "euro area", "ecb"),
    "us": ("united states", "u.s.", "federal reserve"),
    "uk": ("uk", "united kingdom", "britain", "bank of england"),
    "asia": ("asia", "china", "taiwan", "japan", "korea", "hong kong"),
}
REGION_ALIASES = {
    "eu": "europe", "europe": "europe",
    "gb": "uk", "uk": "uk",
    "us": "us", "usa": "us",
    "asia": "asia",
}
TICKER_CHARACTER = r"[A-Za-z0-9.^_=/+\-]"


class RankedEvent(WorldEvent):
    relevance: int = 0
    reasons: list[str] = Field(default_factory=list)
    matched_symbols: list[str] = Field(default_factory=list)


def _contains(text: str, phrase: str, *, ignore_case: bool = True) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", text,
                          re.IGNORECASE if ignore_case else 0))


def _contains_ticker(text: str, ticker: str, *, cashtag: bool = False) -> bool:
    token = f"${ticker}" if cashtag else ticker
    return bool(re.search(
        rf"(?<!{TICKER_CHARACTER}){re.escape(token)}(?!{TICKER_CHARACTER})",
        text,
    ))


def book_symbols(positions: list[dict]) -> list[str]:
    symbols = set()
    for position in positions:
        if not position.get("qty") or position.get("sec_type") in {"CASH", "BAG"}:
            continue
        symbol = str(position.get("symbol", "")).strip().upper()
        # OCC/OSI contract local symbols; normal IBKR contract.symbol already
        # names the underlier. Reject unknown long contract syntax, don't guess.
        if position.get("sec_type") == "OPT":
            osi = re.fullmatch(r"([A-Z.]{1,6})\s*\d{6}[CP]\d{8}", symbol)
            if osi:
                symbol = osi.group(1)
        if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
            symbols.add(symbol)
    return sorted(symbols)


def _mention(text: str, symbol: str) -> str | None:
    if _contains_ticker(text, symbol, cashtag=True):
        return "cashtag mentioned"
    if any(_contains(text, alias) for alias in COMPANY_NAMES.get(symbol, ())):
        return "company name mentioned"
    if len(symbol) >= 3 and symbol not in AMBIGUOUS and _contains_ticker(text, symbol):
        return "ticker mentioned"
    return None


def rank_events(events: list[WorldEvent], symbols: list[str], profile: WorldProfile,
                now: datetime) -> list[RankedEvent]:
    holdings = set(symbols)
    watch = set(profile.watch_symbols) - holdings
    ranked = []
    for event in events:
        text = f"{event.title} {event.summary}"
        reasons, matched = [], []
        score = 0
        for label, candidates, weight in (("Holding", holdings, 70), ("Watchlist", watch, 45)):
            for symbol in sorted(candidates):
                mention = _mention(text, symbol)
                if mention:
                    reasons.append(f"{label} {symbol}: {mention}")
                    matched.append(symbol)
                    score += weight
        topic_text = text + " " + " ".join(event.topics)
        for interest in profile.interests:
            if any(_contains(topic_text, word) for word in TOPIC_WORDS.get(interest.casefold(), (interest,))):
                reasons.append(f"Interest: {interest}")
                score += 15
        event_regions = {
            REGION_ALIASES.get(value.casefold(), value.casefold())
            for value in event.regions
        }
        for region in profile.regions:
            region_key = REGION_ALIASES.get(region.casefold(), region.casefold())
            metadata_match = region_key in event_regions
            text_match = any(
                _contains(text, word)
                for word in REGION_WORDS.get(region_key, (region,))
            )
            if metadata_match or text_match:
                reasons.append(f"Region: {region}")
                score += 5
        ranked.append(RankedEvent(**event.model_dump(), relevance=min(score, 100),
                                  reasons=reasons, matched_symbols=matched))
    # Personal relevance first; then publication/first-observation and stable id.
    # Time is never a fabricated personal reason or a risk/probability estimate.
    return sorted(ranked, key=lambda item: (-item.relevance,
        -datetime.fromisoformat(item.published_at.replace("Z", "+00:00")).timestamp(), item.id))
