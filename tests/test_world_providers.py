from __future__ import annotations

import gzip
import json
from base64 import b64decode
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from quantmind.world.models import WorldConfig, WorldProfile
from quantmind.world.providers import ProviderError, fetch_source, parse_json, parse_xml
from quantmind.world.sources import SOURCES, Source, source_enabled, source_setup_note


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def source(*, kind: str = "rss", url: str = "https://example.gov/feed.xml") -> Source:
    return Source("test", "Test source", "macro", "https://example.gov", url, kind, ("macro",), ("US",), "free", "Official test feed")


def test_profile_normalizes_deduplicates_and_enforces_limits() -> None:
    profile = WorldProfile(watch_symbols=[" aapl ", "AAPL", " brk.b "], interests=[" Inflation ", "inflation"], regions=[" eu "])
    assert profile.model_dump() == {"watch_symbols": ["AAPL", "BRK.B"], "interests": ["inflation"], "regions": ["EU"]}
    with pytest.raises(ValidationError):
        WorldProfile(watch_symbols=[f"S{i}" for i in range(101)])
    with pytest.raises(ValidationError):
        WorldProfile(watch_symbols=["NOT A SYMBOL"])
    with pytest.raises(ValidationError):
        WorldProfile(watch_symbols=["AAPL"], typo="ignored")


def test_event_contract_rejects_invalid_or_unbounded_cached_values() -> None:
    from urllib.parse import urlunsplit

    from quantmind.world.models import WorldEvent

    valid = {
        "id": "event", "source_id": "fed", "source_name": "Federal Reserve",
        "title": "Policy update", "url": "https://example.org/update", "summary": "",
        "published_at": "2026-09-05T10:00:00Z", "topics": [], "regions": [],
    }
    with pytest.raises(ValidationError, match="published_at"):
        WorldEvent(**{**valid, "published_at": "not-a-date"})
    with pytest.raises(ValidationError, match="published_at"):
        WorldEvent(**{**valid, "published_at": "2026-09-05T10:00:00"})
    with pytest.raises(ValidationError, match="title"):
        WorldEvent(**{**valid, "title": "x" * 301})
    for bad_url in (
        "javascript:alert(1)", "http://localhost/a", "http://127.0.0.1/a",
        urlunsplit(("https", "example-user:example-password@example.org", "/a", "", "")),
        "https://example.org:bad/a",
        "https://example.org/a\nb",
    ):
        with pytest.raises(ValidationError, match="url"):
            WorldEvent(**{**valid, "url": bad_url})
    event = WorldEvent(**{**valid, "url": "https://example.org/update?utm_source=cache#evidence"})
    assert event.url == "https://example.org/update?utm_source=cache#evidence"


def test_registry_is_bounded_fixed_and_credentials_are_explicitly_gated() -> None:
    assert 14 <= len(SOURCES) <= 18
    assert len({item.id for item in SOURCES}) == len(SOURCES)
    assert all(item.url.startswith("https://") for item in SOURCES)
    config = WorldConfig()
    by_id = {item.id: item for item in SOURCES}
    assert by_id["usgs"].url == "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
    assert not source_enabled(by_id["sec"], config)
    assert "user agent" in (source_setup_note(by_id["sec"], config) or "").lower()
    assert not source_enabled(by_id["x"], config)
    assert "paid" in (source_setup_note(by_id["x"], config) or "").lower()
    assert not source_enabled(by_id["reddit"], config)
    enabled = WorldConfig(x_enabled=True, x_bearer_token="secret", x_query="markets", reddit_enabled=True, reddit_client_id="id", reddit_client_secret="secret", reddit_refresh_token="refresh", reddit_user_agent="quantmind/1", sec_user_agent="QuantMind local contact@example.com")
    assert source_enabled(by_id["x"], enabled)
    assert source_enabled(by_id["reddit"], enabled)
    assert source_enabled(by_id["sec"], enabled)


def test_rss_parser_strips_markup_and_uses_observed_time_when_date_missing() -> None:
    xml = b"""<rss><channel><item><title> Rates &amp; markets </title><link>https://example.gov/a</link><description><![CDATA[<p>Hello <b>world</b></p><script>bad()</script>]]></description></item></channel></rss>"""
    events = parse_xml(source(), xml, NOW)
    assert len(events) == 1
    assert events[0].title == "Rates & markets"
    assert events[0].summary == "Hello world"
    assert "<" not in events[0].summary
    assert events[0].published_at == "2026-09-05T12:00:00Z"
    assert events[0].time_kind == "observed"


def test_atom_parser_accepts_namespaces_and_rejects_bad_future_date_and_unsafe_url() -> None:
    xml = b"""<feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>Good</title><link href='https://example.gov/good'/><updated>2026-09-05T10:00:00Z</updated><summary>safe</summary></entry>
      <entry><title>Future</title><link href='https://example.gov/future'/><updated>2027-01-01T00:00:00Z</updated></entry>
      <entry><title>Bad date</title><link href='https://example.gov/date'/><updated>not-a-date</updated></entry>
      <entry><title>Local</title><link href='http://127.0.0.1/private'/><updated>2026-09-05T10:00:00Z</updated></entry>
    </feed>"""
    events = parse_xml(source(kind="atom"), xml, NOW)
    assert [event.title for event in events] == ["Good"]
    assert events[0].published_at == "2026-09-05T10:00:00Z"


def test_timezone_less_supplied_date_is_rejected() -> None:
    xml = b"<rss><channel><item><title>Ambiguous</title><link>https://example.gov/a</link><pubDate>2026-09-05T10:00:00</pubDate></item></channel></rss>"
    with pytest.raises(ProviderError, match="valid records"):
        parse_xml(source(), xml, NOW)


@pytest.mark.parametrize("kind,root,container,entry", [
    ("rss", "rss", "channel", "item"), ("atom", "feed", None, "entry"),
])
def test_xml_fields_and_entries_are_only_read_at_their_feed_defined_level(kind, root, container, entry) -> None:
    """Descendant metadata cannot replace fields or become a separate feed event."""
    item = (
        f"<{entry}><metadata><title>Injected</title><link>https://example.gov/injected</link>"
        f"<updated>not-a-date</updated><{entry}><title>Nested</title>"
        f"<link>https://example.gov/nested</link></{entry}></metadata>"
        f"<title>Direct</title><link>https://example.gov/direct</link></{entry}>"
    )
    wrapper = f"<{container}>{item}</{container}>" if container else item
    events = parse_xml(source(kind=kind), f"<{root}>{wrapper}</{root}>".encode(), NOW)
    assert [(event.title, event.url) for event in events] == [("Direct", "https://example.gov/direct")]


@pytest.mark.parametrize("kind,template", [
    pytest.param("rss", "<rss><channel><wrapper><item>{fields}</item></wrapper></channel></rss>", id="nested-rss"),
    pytest.param("atom", "<feed xmlns='http://www.w3.org/2005/Atom'><wrapper><entry>{fields}</entry></wrapper></feed>", id="nested-atom"),
    pytest.param("rss", "<rss><channel/><item>{fields}</item></rss>", id="outside-channel"),
    pytest.param("rss", "<rss><channel><entry>{fields}</entry></channel></rss>", id="wrong-type-rss"),
    pytest.param("atom", "<feed><item>{fields}</item></feed>", id="wrong-type-atom"),
])
def test_misplaced_xml_records_are_a_failed_batch_not_a_healthy_empty_feed(kind, template) -> None:
    """Valid-looking fields in a misplaced record must not advance source health."""
    fields = "<title>private-headline</title><link>https://example.gov/misplaced</link>"
    with pytest.raises(ProviderError, match="valid records") as caught:
        parse_xml(source(kind=kind), template.format(fields=fields).encode(), NOW)
    assert "private-headline" not in str(caught.value)


@pytest.mark.parametrize("kind,root,container,entry", [
    ("rss", "rss", "channel", "item"), ("atom", "feed", None, "entry"),
])
def test_misplaced_xml_records_do_not_discard_valid_direct_siblings(kind, root, container, entry) -> None:
    """Misplaced records are rejected individually when the batch has real entries."""
    records = (
        f"<wrapper><{entry}><title>Misplaced</title><link>https://example.gov/misplaced</link></{entry}></wrapper>"
        f"<{entry}><title>Direct</title><link>https://example.gov/direct</link></{entry}>"
    )
    content = f"<{container}>{records}</{container}>" if container else records
    events = parse_xml(source(kind=kind), f"<{root}>{content}</{root}>".encode(), NOW)
    assert [(event.title, event.url) for event in events] == [("Direct", "https://example.gov/direct")]


@pytest.mark.parametrize("kind", ["rss", "gdelt"])
@pytest.mark.parametrize("field", ["title", "summary"])
def test_malformed_html_in_one_record_does_not_discard_valid_neighbors(kind, field) -> None:
    """HTMLParser assertions in untrusted text are confined to that record."""
    if kind == "rss":
        tag = "description" if field == "summary" else "title"
        bad_title = "<title>Bad</title>" if field == "summary" else ""
        body = (
            "<rss><channel><item><title>Before</title><link>https://example.gov/before</link></item>"
            f"<item>{bad_title}<{tag}><![CDATA[<![broken]>]]></{tag}>"
            "<link>https://example.gov/bad</link></item>"
            "<item><title>After</title><link>https://example.gov/after</link></item></channel></rss>"
        ).encode()
        events = parse_xml(source(), body, NOW)
    else:
        bad = {"title": "Bad", "url": "https://example.gov/bad"}
        bad["domain" if field == "summary" else "title"] = "<![broken]>"
        body = json.dumps({"articles": [
            {"title": "Before", "url": "https://example.gov/before"}, bad,
            {"title": "After", "url": "https://example.gov/after"},
        ]}).encode()
        events = parse_json(source(kind=kind), body, NOW)
    assert [event.title for event in events] == ["Before", "After"]


@pytest.mark.parametrize("kind,body", [
    ("rss", b"<rss><channel><item><title>private-headline</title></item></channel></rss>"),
    ("atom", b"<feed><entry><title>private-headline</title></entry></feed>"),
    ("usgs_geojson", b'{"features":[null,{"properties":{"title":"private-headline"}}]}'),
    ("gdelt", b'{"articles":[null,{"title":"private-headline"}]}'),
    ("x", b'{"data":[{"id":"invalid","text":"private-headline"}],"meta":{"result_count":1}}'),
    ("reddit", b'{"data":{"children":[{"data":{"title":"private-headline"}}]}}'),
])
def test_nonempty_all_invalid_batch_is_a_safe_provider_failure(kind, body) -> None:
    parser = parse_xml if kind in {"rss", "atom"} else parse_json
    with pytest.raises(ProviderError, match="valid records") as caught:
        parser(source(kind=kind), body, NOW)
    assert "private-headline" not in str(caught.value)


@pytest.mark.parametrize("xml", [b"<html><body>blocked</body></html>", b"<something/>"])
def test_xml_parser_rejects_non_feed_envelope(xml: bytes) -> None:
    with pytest.raises(ProviderError, match="Invalid XML feed"):
        parse_xml(source(), xml, NOW)


def test_xml_parser_allows_legitimate_empty_feed() -> None:
    assert parse_xml(source(), b"<rss><channel/></rss>", NOW) == []
    assert parse_xml(source(kind="atom"), b"<feed xmlns='http://www.w3.org/2005/Atom'/>", NOW) == []


def test_xml_parser_rejects_entities_and_skips_individually_malformed_records() -> None:
    with pytest.raises(ProviderError, match="Invalid XML feed"):
        parse_xml(source(), b'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&leak;</title></item></channel></rss>', NOW)
    xml = b"<rss><channel><item><title></title><link>https://example.gov/empty</link></item><item><title>Good</title><link>https://example.gov/good</link><pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    assert [event.title for event in parse_xml(source(), xml, NOW)] == ["Good"]


def test_rss_parser_prefers_link_when_non_url_guid_comes_first() -> None:
    xml = b"<rss><channel><item><guid isPermaLink='false'>{opaque}</guid><link>https://example.gov/release</link><title>Release</title><pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    event = parse_xml(source(), xml, NOW)[0]
    assert event.url == "https://example.gov/release"


def test_usgs_and_gdelt_json_have_correct_time_semantics() -> None:
    usgs = source(kind="usgs_geojson", url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson")
    payload = b'{"features":[{"id":"abc","properties":{"title":"M 5.1 - Test","url":"https://earthquake.usgs.gov/earthquakes/eventpage/abc","time":1788602400000,"place":"Test region"}}]}'
    event = parse_json(usgs, payload, NOW)[0]
    assert event.time_kind == "published"
    assert event.summary == "Test region"
    gdelt = source(kind="gdelt", url="https://api.gdeltproject.org/api/v2/doc/doc?query=markets&mode=artlist&format=json")
    payload = b'{"articles":[{"title":"Market update","url":"https://news.example.com/a","seendate":"20260905T103000Z","domain":"news.example.com"}]}'
    event = parse_json(gdelt, payload, NOW)[0]
    assert event.time_kind == "observed"
    assert event.published_at == "2026-09-05T10:30:00Z"


def test_json_parser_rejects_wrong_envelope_but_allows_legitimate_empty_list() -> None:
    usgs = source(kind="usgs_geojson")
    with pytest.raises(ProviderError, match="Invalid JSON feed"):
        parse_json(usgs, b'{"not_features":[]}', NOW)
    assert parse_json(usgs, b'{"features":[]}', NOW) == []


def test_json_parser_skips_malformed_records_and_keeps_valid_adjacent_record() -> None:
    usgs = source(kind="usgs_geojson")
    payload = b'{"features":[null,{"properties":null},{"properties":{"title":"Huge time","url":"https://earthquake.usgs.gov/a","time":999999999999999999999}},{"properties":{"title":"M 5.0 - Safe","url":"https://earthquake.usgs.gov/good?utm_source=x#map","time":1788602400000,"place":"Safe"}}]}'
    events = parse_json(usgs, payload, NOW)
    assert len(events) == 1
    assert events[0].url == "https://earthquake.usgs.gov/good"


def test_reddit_parser_skips_out_of_range_timestamp_and_keeps_valid_post() -> None:
    reddit = source(kind="reddit")
    payload = b'{"data":{"children":[{"data":{"title":"Bad","permalink":"/r/investing/comments/bad123/bad/","created_utc":999999999999999999999}},{"data":{"title":"Good","permalink":"/r/investing/comments/good123/good/","created_utc":1788602400}}]}}'
    events = parse_json(reddit, payload, NOW)
    assert [event.title for event in events] == ["Good"]


def test_x_parser_skips_missing_wrong_type_and_nonnumeric_ids_beside_valid_post() -> None:
    x = source(kind="x")
    payload = b'{"data":[{"text":"Missing"},{"id":"","text":"Empty"},{"id":123,"text":"Wrong type"},{"id":"abc","text":"Nonnumeric"},{"id":"1844674407370955161","text":"Valid","created_at":"2026-09-05T10:00:00Z"}],"meta":{"result_count":5}}'
    events = parse_json(x, payload, NOW)
    assert [(event.title, event.url) for event in events] == [
        ("Valid", "https://x.com/i/web/status/1844674407370955161"),
    ]


def test_x_parser_accepts_documented_empty_search_envelope_only_when_count_is_zero() -> None:
    x = source(kind="x")
    assert parse_json(x, b'{"meta":{"result_count":0}}', NOW) == []
    for malformed in (
        b'{"meta":{"result_count":1}}',
        b'{"meta":{"result_count":"0"}}',
        b'{"meta":null}',
        b'{"errors":[{"title":"Invalid Request"}],"meta":{"result_count":0}}',
    ):
        with pytest.raises(ProviderError, match="Invalid JSON feed"):
            parse_json(x, malformed, NOW)


def test_reddit_parser_skips_missing_wrong_type_and_malformed_permalink_beside_valid_post() -> None:
    reddit = source(kind="reddit")
    payload = b'{"data":{"children":[{"data":{"title":"Missing","created_utc":1788602400}},{"data":{"title":"Empty","permalink":"","created_utc":1788602400}},{"data":{"title":"Wrong type","permalink":7,"created_utc":1788602400}},{"data":{"title":"Absolute","permalink":"https://evil.test/r/x/comments/a/b/","created_utc":1788602400}},{"data":{"title":"Traversal","permalink":"/r/x/../../private","created_utc":1788602400}},{"data":{"title":"Valid","permalink":"/r/investing/comments/abc123/market_update/","created_utc":1788602400}}]}}'
    events = parse_json(reddit, payload, NOW)
    assert [(event.title, event.url) for event in events] == [
        ("Valid", "https://www.reddit.com/r/investing/comments/abc123/market_update/"),
    ]


def test_reddit_parser_accepts_unicode_and_percent_encoded_title_slug() -> None:
    reddit = source(kind="reddit")
    payload = '{"data":{"children":[{"data":{"title":"Valid","permalink":"/r/investing/comments/abc123/caf%C3%A9_✓/","created_utc":1788602400}}]}}'.encode()
    event = parse_json(reddit, payload, NOW)[0]
    assert event.url == "https://www.reddit.com/r/investing/comments/abc123/caf%C3%A9_✓/"


def test_canonical_url_keeps_meaningful_query_but_drops_tracking_and_fragment() -> None:
    xml = b"<rss><channel><item><title>Release</title><link>https://example.gov/a?id=7&amp;utm_campaign=email#top</link></item></channel></rss>"
    assert parse_xml(source(), xml, NOW)[0].url == "https://example.gov/a?id=7"


@pytest.mark.asyncio
async def test_fetch_streams_with_no_redirects_and_rejects_oversize_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://example.gov/feed.xml"
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(ProviderError, match="too large"):
            await fetch_source(source(), client, WorldConfig(), NOW)


@pytest.mark.asyncio
async def test_fetch_rejects_declared_oversize_body_before_reading() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(2 * 1024 * 1024 + 1)}, content=b"small")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="too large"):
            await fetch_source(source(), client, WorldConfig(), NOW)


@pytest.mark.asyncio
async def test_fetch_rejects_encoded_response_before_reading_or_decoding_stream() -> None:
    read = False
    compressed = gzip.compress(b"x" * (16 * 1024 * 1024))

    class EncodedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read
            read = True
            yield compressed

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=EncodedStream(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="encoded response"):
            await fetch_source(source(), client, WorldConfig(), NOW)
    assert not read


@pytest.mark.asyncio
async def test_reddit_token_response_uses_same_body_cap() -> None:
    reddit = next(item for item in SOURCES if item.id == "reddit")
    cfg = WorldConfig(reddit_enabled=True, reddit_client_id="id", reddit_client_secret="secret", reddit_refresh_token="refresh", reddit_user_agent="quantmind/1")
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        raise AssertionError("listing request should not occur")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="too large"):
            await fetch_source(reddit, client, cfg, NOW)


@pytest.mark.asyncio
async def test_fetch_reports_safe_http_error_and_retry_after() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, content=b"token=secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(ProviderError) as caught:
            await fetch_source(source(), client, WorldConfig(), NOW)
    assert str(caught.value) == "Source returned HTTP 429"
    assert caught.value.retry_after == 30
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_fetch_supports_http_date_retry_after_and_requests_identity_encoding() -> None:
    retry_at = datetime.now(UTC).replace(microsecond=0).timestamp() + 30
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(503, headers={"Retry-After": format_datetime(datetime.fromtimestamp(retry_at, UTC), usegmt=True)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            await fetch_source(source(), client, WorldConfig(), NOW)
    assert caught.value.retry_after is not None
    assert 28 <= caught.value.retry_after <= 30


@pytest.mark.asyncio
async def test_reddit_oauth_and_listing_share_one_whole_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import quantmind.world.providers as providers
    reddit = next(item for item in SOURCES if item.id == "reddit")
    cfg = WorldConfig(reddit_enabled=True, reddit_client_id="id", reddit_client_secret="secret", reddit_refresh_token="refresh", reddit_user_agent="quantmind/1")
    monkeypatch.setattr(providers, "WHOLE_REQUEST_TIMEOUT", 0.01)
    async def handler(request: httpx.Request) -> httpx.Response:
        await __import__("asyncio").sleep(0.008)
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"data": {"children": []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="timed out"):
            await fetch_source(reddit, client, cfg, NOW)


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_instead_of_following_it() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(ProviderError, match="redirect"):
            await fetch_source(source(), client, WorldConfig(), NOW)


async def test_enabled_x_fetch_uses_explicit_query_and_bearer_without_losing_publication_time() -> None:
    """Wrong auth/query/fields must fail the outbound contract, not look like empty news."""
    x = next(item for item in SOURCES if item.id == "x")
    config = WorldConfig(x_enabled=True, x_bearer_token="synthetic-x-token",
                         x_query="  (from:example_news OR semiconductor) -is:retweet  ")
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.x.com"
        assert request.url.path == "/2/tweets/search/recent"
        assert request.headers["Authorization"] == "Bearer synthetic-x-token"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Accept-Encoding"] == "identity"
        assert dict(request.url.params) == {
            "query": "(from:example_news OR semiconductor) -is:retweet",
            "max_results": "100",
            "tweet.fields": "created_at",
        }
        return httpx.Response(200, json={
            "data": [{"id": "123456789", "text": "Semiconductor capacity update",
                      "created_at": "2026-09-05T10:00:00Z"}],
            "meta": {"result_count": 1, "newest_id": "123456789", "oldest_id": "123456789"},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await fetch_source(x, client, config, NOW)

    assert len(requests) == 1
    assert [(event.source_id, event.title, event.url, event.published_at, event.time_kind)
            for event in events] == [
        ("x", "Semiconductor capacity update", "https://x.com/i/web/status/123456789",
         "2026-09-05T10:00:00Z", "published"),
    ]
    assert "synthetic-x-token" not in str(events)


@pytest.mark.parametrize("access_token", ["synthetic-access", "aZ09-._~+/=="])
async def test_enabled_reddit_fetch_exchanges_refresh_grant_then_reads_configured_subreddits(access_token: str) -> None:
    """OAuth credentials belong on the token POST; only the issued bearer goes to listings."""
    reddit = next(item for item in SOURCES if item.id == "reddit")
    config = WorldConfig(
        reddit_enabled=True, reddit_client_id="synthetic-client",
        reddit_client_secret="synthetic-secret", reddit_refresh_token="synthetic-refresh",
        reddit_user_agent="QuantMind-test/1.0 (synthetic fixture)",
        reddit_subreddits=" investing , stocks,investing,Economics ",
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["User-Agent"] == "QuantMind-test/1.0 (synthetic fixture)"
        if len(requests) == 1:
            assert request.method == "POST"
            assert str(request.url) == "https://www.reddit.com/api/v1/access_token"
            scheme, encoded = request.headers["Authorization"].split(" ", 1)
            assert scheme == "Basic"
            assert b64decode(encoded).decode() == "synthetic-client:synthetic-secret"
            assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["refresh_token"], "refresh_token": ["synthetic-refresh"],
            }
            return httpx.Response(200, json={
                "access_token": access_token, "token_type": "bearer",
                "expires_in": 3600, "scope": "read",
            })
        assert len(requests) == 2
        assert request.method == "GET"
        assert str(request.url) == "https://oauth.reddit.com/r/investing+stocks+Economics/new"
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.content == b""
        return httpx.Response(200, json={"kind": "Listing", "data": {
            "after": None, "before": None, "dist": 1,
            "children": [{"kind": "t3", "data": {
                "id": "abc123", "title": "Market discussion", "selftext": "Rates and growth",
                "permalink": "/r/investing/comments/abc123/market_discussion/",
                "created_utc": 1788602400,
            }}],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await fetch_source(reddit, client, config, NOW)

    assert len(requests) == 2
    assert [(event.source_id, event.title, event.summary, event.url,
             event.published_at, event.time_kind) for event in events] == [
        ("reddit", "Market discussion", "Rates and growth",
         "https://www.reddit.com/r/investing/comments/abc123/market_discussion/",
         "2026-09-05T10:00:00Z", "published"),
    ]
    assert "synthetic-" not in str(events)
    assert access_token not in str(events)


@pytest.mark.parametrize("body", [
    pytest.param(b"{}", id="missing-token"),
    pytest.param(b"not-json", id="invalid-json"),
    pytest.param(b'{"access_token":{"private":"not-a-token"}}', id="non-string-token"),
    pytest.param(b'{"access_token":123}', id="numeric-token"),
    pytest.param(b'{"access_token":["private"]}', id="array-token"),
    pytest.param(b'{"access_token":""}', id="empty-token"),
    pytest.param(b'{"access_token":"   "}', id="blank-token"),
    pytest.param(b'{"access_token":"private token"}', id="space-in-token"),
    pytest.param(b'{"access_token":"private\\r\\nheader"}', id="control-in-token"),
    pytest.param(b'{"access_token":"private\\u00e9"}', id="non-ascii-token"),
])
async def test_malformed_reddit_oauth_stops_before_listing_without_exposing_response(body: bytes) -> None:
    """Malformed authorization must fail closed instead of becoming a listing credential."""
    reddit = next(item for item in SOURCES if item.id == "reddit")
    config = WorldConfig(
        reddit_enabled=True, reddit_client_id="synthetic-client",
        reddit_client_secret="synthetic-secret", reddit_refresh_token="synthetic-refresh",
        reddit_user_agent="QuantMind-test/1.0",
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"kind": "Listing", "data": {"children": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError, match="Reddit authorization") as caught:
            await fetch_source(reddit, client, config, NOW)

    assert len(requests) == 1
    assert "private" not in str(caught.value)
    assert "synthetic" not in str(caught.value)


def test_reddit_subreddit_config_rejects_empty_or_unsafe_listing_paths() -> None:
    """Configured names cannot inject a new path/query or evade the ten-community bound."""
    for names in (" , ", "investing/../../private", "investing?limit=100", "investing+stocks",
                  "x" * 22, ",".join(f"community{i}" for i in range(11))):
        with pytest.raises(ValidationError, match="subreddit"):
            WorldConfig(reddit_subreddits=names)
