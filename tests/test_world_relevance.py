"""Hand-checked relevance reasons, not inferred exposures or investment advice."""
from datetime import datetime, timezone

import pytest


def rank(title, *, symbols=(), watch=(), interests=(), regions=(), topics=(), summary=""):
    from quantmind.world.models import WorldEvent, WorldProfile
    from quantmind.world.relevance import rank_events
    event = WorldEvent(id="a", source_id="fed", source_name="Federal Reserve", title=title,
                       url="https://example.org/news", summary=summary,
                       published_at="2026-09-05T10:00:00+00:00", topics=list(topics), regions=[])
    return rank_events([event], list(symbols), WorldProfile(
        watch_symbols=list(watch), interests=list(interests), regions=list(regions)),
        datetime(2026, 9, 5, 12, tzinfo=timezone.utc))[0]


def test_company_name_is_an_explicit_holding_reason_not_an_exposure_estimate():
    result = rank("Nvidia announces new processors", symbols=["NVDA"])
    assert result.matched_symbols == ["NVDA"]
    assert "Holding NVDA: company name mentioned" in result.reasons
    assert result.relevance > 0


def test_common_word_and_short_tickers_need_cashtags_or_company_names():
    result = rank("AI is changing IT and the global economy", symbols=["AI", "IT", "ON"])
    assert result.matched_symbols == []
    assert result.reasons == []
    tagged = rank("$AI earnings after close", symbols=["AI"])
    assert tagged.matched_symbols == ["AI"]


def test_watchlist_match_is_never_labeled_as_a_holding():
    result = rank("ASML supplies chip equipment", watch=["ASML"])
    assert result.matched_symbols == ["ASML"]
    assert all(not reason.startswith("Holding") for reason in result.reasons)
    assert any(reason.startswith("Watchlist ASML") for reason in result.reasons)


def test_interest_and_region_matches_are_explained_separately():
    result = rank("Europe semiconductor supply outlook", interests=["semiconductors"], regions=["Europe"])
    assert "Interest: semiconductors" in result.reasons
    assert "Region: EUROPE" in result.reasons
    assert not result.matched_symbols


def test_no_interests_or_book_does_not_fabricate_a_personal_lens():
    assert rank("Federal Reserve announces interest rate decision", topics=["rates"]).relevance == 0


def test_quoted_symbol_boundaries_and_case_do_not_match_inside_other_words():
    assert rank("anvda and nvda research", symbols=["NVDA"]).matched_symbols == []
    assert rank("NASDAQ:NVDA results", symbols=["NVDA"]).matched_symbols == ["NVDA"]


def test_symbol_does_not_match_prefix_of_a_longer_punctuated_ticker():
    for headline in (
        "$BRK.B announces results",
        "$BRK-B announces results",
        "$BRK=F announces results",
        "$BRK^A announces results",
        "$BRK/A announces results",
        "$BRK+A announces results",
        "$BRK_A announces results",
        "BRK.B announces results",
        "BRKfoo announces results",
        "xBRK announces results",
    ):
        assert rank(headline, symbols=["BRK"]).matched_symbols == []

    assert rank("$BRK announces results", symbols=["BRK"]).matched_symbols == ["BRK"]


@pytest.mark.parametrize(("headline", "symbol", "reason"), [
    ("Results lifted $BP.", "BP", "Holding BP: cashtag mentioned"),
    ("Investors bought NVDA.", "NVDA", "Holding NVDA: ticker mentioned"),
    ("Investors bought NVDA. More detail follows.", "NVDA", "Holding NVDA: ticker mentioned"),
    ("The position was $BRK.B.", "BRK.B", "Holding BRK.B: cashtag mentioned"),
    ("A position in $AI.", "AI", "Holding AI: cashtag mentioned"),
    ("A position in $ARM.)", "ARM", "Holding ARM: cashtag mentioned"),
])
def test_sentence_period_does_not_hide_an_exact_ticker(headline, symbol, reason):
    """Treat sentence punctuation as punctuation without guessing share classes."""
    result = rank(headline, symbols=[symbol])
    assert result.matched_symbols == [symbol]
    assert result.reasons == [reason]
    assert result.relevance == 70


@pytest.mark.parametrize("headline", [
    "NVDA.AS", "$NVDA.AS", "NVDA..AS", "$NVDA.AS.", "NVDA-B",
    "NVDA=F", "NVDA^A", "NVDA/A", "NVDA+A", "NVDA_A", "NVDAfoo", "xNVDA",
])
def test_sentence_period_fix_never_matches_a_different_ticker_suffix(headline):
    result = rank(headline, symbols=["NVDA"])
    assert result.matched_symbols == []
    assert result.reasons == []
    assert result.relevance == 0


def test_option_underlying_is_normalized_without_netting_away_offsetting_legs():
    from quantmind.world.relevance import book_symbols
    assert book_symbols([
        {"symbol": "NVDA  260918C00150000", "sec_type": "OPT", "qty": 2},
        {"symbol": "NVDA", "sec_type": "OPT", "qty": -2},
        {"symbol": "ASML", "sec_type": "STK", "qty": 3},
        {"symbol": "MSFT", "sec_type": "STK", "qty": 0},
        {"symbol": "EUR", "sec_type": "CASH", "qty": 1000},
    ]) == ["ASML", "NVDA"]


def test_unknown_etf_has_no_invented_lookthrough():
    assert rank("Nvidia issues results", symbols=["IWDA"]).matched_symbols == []


def test_region_metadata_uses_canonical_aliases_without_matching_us_pronoun():
    from quantmind.world.models import WorldEvent, WorldProfile
    from quantmind.world.relevance import rank_events

    def ranked(title, event_region, profile_region):
        item = WorldEvent(
            id=event_region, source_id="source", source_name="Source", title=title,
            url="https://example.org/news", summary="",
            published_at="2026-09-05T10:00:00Z", topics=[], regions=[event_region],
        )
        return rank_events(
            [item], [], WorldProfile(regions=[profile_region]),
            datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
        )[0]

    assert "Region: EUROPE" in ranked("Policy update", "EU", "Europe").reasons
    assert "Region: UK" in ranked("Policy update", "GB", "UK").reasons
    assert "Region: LATAM" in ranked("Policy update", "latam", "LATAM").reasons
    assert "Region: US" not in ranked("Officials tell us more", "GLOBAL", "US").reasons


@pytest.mark.parametrize(("headline", "symbols", "watch", "expected"), [
    ("Nvidia $NVDA.", ["NVDA"], ["NVDA"], ["Holding NVDA: cashtag mentioned"]),
    ("Nvidia and NVDA report.", ["NVDA"], [], ["Holding NVDA: company name mentioned"]),
    ("NASDAQ:NVDA", [], ["NVDA"], ["Watchlist NVDA: ticker mentioned"]),
    ("BP AI IT ARM SAFE ON META", ["BP", "IT", "ON"], ["AI", "ARM", "SAFE", "META"], []),
    ("nvda $nvda xNVDA NVDA.AS", ["NVDA"], [], []),
    ("Nestlé expands", ["NESN.SW"], [], ["Holding NESN.SW: company name mentioned"]),
    ("SİEMENS and ſiemens expand", [], ["SIE.DE"], ["Watchlist SIE.DE: company name mentioned"]),
    ("Nestléish preſiemenspost", ["NESN.SW"], ["SIE.DE"], []),
    ("Facebook reports", ["META"], [], ["Holding META: company name mentioned"]),
    ("$BRK.B. and $AI.", ["BRK.B"], ["AI"], [
        "Holding BRK.B: cashtag mentioned", "Watchlist AI: cashtag mentioned",
    ]),
])
def test_ranking_optimization_preserves_literal_identity_and_reason_precedence(
    headline, symbols, watch, expected,
):
    """Optimizing scans must not change Unicode aliases, boundaries, or reason priority."""
    result = rank(headline, symbols=symbols, watch=watch)
    assert result.reasons == expected


def test_ranking_preserves_independent_reasons_capped_scores_and_stable_event_order():
    from quantmind.world.models import WorldEvent, WorldProfile
    from quantmind.world.relevance import rank_events

    def event(key, title, timestamp="2026-09-05T10:00:00Z", **changes):
        return WorldEvent(
            id=key, source_id="fed", source_name="Federal Reserve", title=title,
            url=f"https://example.org/{key}", summary="", published_at=timestamp,
            topics=changes.get("topics", []), regions=changes.get("regions", []),
        )

    events = [
        event("z", "Policy update", "2026-09-05T11:00:00Z"),
        event("old", "$BP. and ASML chip supplies in Europe", "2026-09-05T08:00:00Z"),
        event("b", "Nvidia and NVDA discuss $BP. ASML chip supplies for Europe."),
        event("topic", "Policy update", topics=["semiconductors"], regions=["EU"]),
        event("a", "Nvidia and NVDA discuss $BP. ASML chip supplies for Europe."),
    ]
    symbols = ["NVDA", "BP", "NVDA"]
    profile = WorldProfile(watch_symbols=["NVDA", "ASML"], interests=["semiconductors"], regions=["Europe"])
    result = rank_events(events, symbols, profile, datetime(2026, 9, 5, 12, tzinfo=timezone.utc))

    assert [(item.id, item.relevance, item.matched_symbols) for item in result] == [
        ("a", 100, ["BP", "NVDA", "ASML"]),
        ("b", 100, ["BP", "NVDA", "ASML"]),
        ("old", 100, ["BP", "ASML"]),
        ("topic", 20, []),
        ("z", 0, []),
    ]
    assert result[0].reasons == [
        "Holding BP: cashtag mentioned", "Holding NVDA: company name mentioned",
        "Watchlist ASML: company name mentioned", "Interest: semiconductors", "Region: EUROPE",
    ]
    assert result[3].reasons == ["Interest: semiconductors", "Region: EUROPE"]
    assert result[4].reasons == []
    assert all(not getattr(item, "reasons", None) for item in events)
    assert symbols == ["NVDA", "BP", "NVDA"]  # No input mutation or hidden retained ranking.
    assert profile.watch_symbols == ["NVDA", "ASML"]


def test_absent_watch_symbols_do_not_trigger_a_regex_scan_per_candidate(monkeypatch):
    """Guard the measured full-lens cost by counting real regex scans, not wall time.

    Reintroducing unconditional regex searches for every absent watch ticker
    breaches this deterministic budget. Every counted search still executes
    the real regex engine, and assertions below also check real ranking output.
    """
    from pathlib import Path
    import re
    import runpy
    import quantmind.world.relevance as relevance

    scans = 0

    class CountingRegex:
        def __getattr__(self, name):
            return getattr(re, name)

        def search(self, *args, **kwargs):
            nonlocal scans
            scans += 1
            return re.search(*args, **kwargs)

    benchmark = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/world_ranking_benchmark.py"))
    events, symbols, profile, now = benchmark["fixture"]()
    monkeypatch.setattr(relevance, "re", CountingRegex())
    result = relevance.rank_events(events, symbols, profile, now)

    assert len(result) == 420
    assert all(item.relevance == 0 and item.reasons == [] and item.matched_symbols == [] for item in result)
    # Topic/region and known company-alias checks remain; 100 absent watch
    # symbols must not each add two full-text regex searches to every row.
    assert scans <= len(events) * 60, f"{scans} regex scans exceeded the bounded full-lens budget"
