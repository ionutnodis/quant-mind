"""Hand-checked relevance reasons, not inferred exposures or investment advice."""
from datetime import datetime, timezone


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
