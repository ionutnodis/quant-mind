from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree.ElementTree import TreeBuilder

import httpx
from defusedxml import ElementTree

from .models import WorldConfig, WorldEvent
from .sources import Source, source_enabled, source_setup_note
from .urls import canonicalize_public_http_url

MAX_BODY = 2 * 1024 * 1024
MAX_EVENTS = 200
MAX_XML_DEPTH = 32
MAX_XML_NODES = 10_000
MAX_FIELD_TEXT = 16 * 1024
WHOLE_REQUEST_TIMEOUT = 12.0


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _BoundedTreeBuilder(TreeBuilder):
    """Reject pathological structure while building, before allocating its full tree."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.has_records = False

    def start(self, tag, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > MAX_XML_DEPTH or self.nodes > MAX_XML_NODES:
            raise ProviderError("XML feed was too complex")
        if tag[tag.rfind("}") + 1:].lower() in {"item", "entry"}:
            self.has_records = True
        return super().start(tag, attrs)

    def end(self, tag):
        node = super().end(tag)
        self.depth -= 1
        return node


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _plain(value: str | None, limit: int) -> str:
    if value is not None and len(value) > MAX_FIELD_TEXT:
        raise ValueError("Feed text was too large")
    parser = _TextExtractor()
    parser.feed(html.unescape(value or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def _safe_url(value: str | None) -> str | None:
    return canonicalize_public_http_url(value or "")


def _date(value: object, now: datetime, *, milliseconds: bool = False) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if milliseconds:
            result = datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        else:
            text = str(value).strip()
            if re.fullmatch(r"\d{8}T\d{6}Z", text):
                result = datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", text):
                result = datetime.fromisoformat(text.replace("Z", "+00:00"))
            else:
                result = parsedate_to_datetime(text)
            if result.tzinfo is None:
                return None
            result = result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return result if result <= now else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _event(source: Source, *, title: object, url: object, summary: object, supplied_date: object, now: datetime, observed: bool = False, milliseconds: bool = False) -> WorldEvent | None:
    # The sanitizer and model validation process one untrusted record. A bad
    # headline/summary must never discard valid siblings or expose its contents.
    try:
        clean_title = _plain(str(title or ""), 300)
        clean_url = _safe_url(str(url or ""))
        if not clean_title or not clean_url:
            return None
        timestamp = _date(supplied_date, now, milliseconds=milliseconds)
        if supplied_date not in (None, "") and timestamp is None:
            return None
        time_kind = "observed" if observed or timestamp is None else "published"
        timestamp = timestamp or now
        event_id = hashlib.sha256(f"{source.id}\0{clean_url}".encode()).hexdigest()[:32]
        return WorldEvent(id=event_id, source_id=source.id, source_name=source.name, title=clean_title, url=clean_url, summary=_plain(str(summary or ""), 500), published_at=_iso(timestamp), time_kind=time_kind, topics=list(source.topics), regions=list(source.regions))
    except (AssertionError, ValueError, TypeError, RecursionError):
        return None


def _child_text(node: ElementTree.Element, names: tuple[str, ...]) -> str | None:
    # RSS/Atom fields belong directly to an item/entry, not its descendants.
    for child in node:
        if child.tag.rsplit("}", 1)[-1].lower() in names and child.text:
            return child.text
    return None


def _valid_reddit_permalink(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    match = re.fullmatch(
        r"/r/[A-Za-z0-9_]{1,21}/comments/[A-Za-z0-9]+/([^/?#]+)/?",
        value,
    )
    if not match or re.search(r"%(?![0-9A-Fa-f]{2})", match.group(1)):
        return None
    return value


def parse_xml(source: Source, body: bytes, now: datetime) -> list[WorldEvent]:
    """Parse bounded RSS channel/items or Atom feed/entries, never nested records."""
    if len(body) > MAX_BODY:
        raise ProviderError("Source response was too large")
    try:
        builder = _BoundedTreeBuilder()
        parser = ElementTree.DefusedXMLParser(target=builder, forbid_dtd=True)
        parser.feed(body)
        root = parser.close()
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("Invalid XML feed") from None
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    if root_name not in {"rss", "feed"}:
        raise ProviderError("Invalid XML feed")
    if root_name == "rss":
        channels = [node for node in root if node.tag.rsplit("}", 1)[-1].lower() == "channel"]
        if len(channels) != 1:
            raise ProviderError("Invalid XML feed")
        container, entry_name = channels[0], "item"
    else:
        container, entry_name = root, "entry"
    entries = [node for node in container if node.tag.rsplit("}", 1)[-1].lower() == entry_name]
    events: list[WorldEvent] = []
    for node in entries[:MAX_EVENTS]:
        link = _child_text(node, ("link",)) or _child_text(node, ("guid",))
        for child in node:
            if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                link = child.attrib["href"]
                break
        date_value = _child_text(node, ("pubdate", "published", "updated", "date"))
        event = _event(source, title=_child_text(node, ("title",)), url=link, summary=_child_text(node, ("description", "summary", "content")), supplied_date=date_value, now=now)
        if event:
            events.append(event)
    # Misplaced records are not admitted, but cannot make a malformed batch
    # appear successfully empty. Track their presence during the bounded build.
    if builder.has_records and not events:
        raise ProviderError("Source feed contained no valid records")
    return events


def parse_json(source: Source, body: bytes, now: datetime) -> list[WorldEvent]:
    if len(body) > MAX_BODY:
        raise ProviderError("Source response was too large")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        raise ProviderError("Invalid JSON feed") from None
    events: list[WorldEvent] = []
    records: list[object] = []
    if source.kind == "usgs_geojson":
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise ProviderError("Invalid JSON feed")
        records = payload["features"]
        for record in records[:MAX_EVENTS]:
            if not isinstance(record, dict) or not isinstance(record.get("properties"), dict):
                continue
            props = record["properties"]
            event = _event(source, title=props.get("title"), url=props.get("url"), summary=props.get("place"), supplied_date=props.get("time"), now=now, milliseconds=True)
            if event:
                events.append(event)
    elif source.kind == "gdelt":
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise ProviderError("Invalid JSON feed")
        records = payload["articles"]
        for record in records[:MAX_EVENTS]:
            if not isinstance(record, dict):
                continue
            event = _event(source, title=record.get("title"), url=record.get("url"), summary=record.get("domain"), supplied_date=record.get("seendate"), now=now, observed=True)
            if event:
                events.append(event)
    elif source.kind == "x":
        if not isinstance(payload, dict) or "errors" in payload:
            raise ProviderError("Invalid JSON feed")
        meta = payload.get("meta")
        result_count = meta.get("result_count") if isinstance(meta, dict) else None
        if type(result_count) is not int or result_count < 0:
            raise ProviderError("Invalid JSON feed")
        records = payload.get("data")
        if records is None and result_count == 0:
            return []
        if not isinstance(records, list) or result_count != len(records):
            raise ProviderError("Invalid JSON feed")
        for record in records[:MAX_EVENTS]:
            if not isinstance(record, dict):
                continue
            post_id = record.get("id")
            if not isinstance(post_id, str) or not re.fullmatch(r"[1-9]\d{0,24}", post_id):
                continue
            event = _event(source, title=record.get("text"), url=f"https://x.com/i/web/status/{post_id}", summary="", supplied_date=record.get("created_at"), now=now)
            if event:
                events.append(event)
    elif source.kind == "reddit":
        data = payload.get("data") if isinstance(payload, dict) else None
        records = data.get("children") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise ProviderError("Invalid JSON feed")
        for wrapper in records[:MAX_EVENTS]:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), dict):
                continue
            record = wrapper.get("data", {})
            permalink = _valid_reddit_permalink(record.get("permalink"))
            if permalink is None:
                continue
            created = record.get("created_utc")
            if isinstance(created, (int, float)):
                try:
                    supplied = datetime.fromtimestamp(created, tz=UTC).isoformat()
                except (OverflowError, OSError, ValueError):
                    continue
            else:
                supplied = created
            event = _event(source, title=record.get("title"), url=f"https://www.reddit.com{permalink}", summary=record.get("selftext"), supplied_date=supplied, now=now)
            if event:
                events.append(event)
    if records and not events:
        raise ProviderError("Source feed contained no valid records")
    return events


async def _bounded_response(client: httpx.AsyncClient, method: str, url: str, **kwargs: object) -> bytes:
    try:
        async with asyncio.timeout(12):
            async with client.stream(method, url, timeout=8, follow_redirects=False, **kwargs) as response:
                if response.is_redirect:
                    raise ProviderError("Source returned a redirect")
                content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
                if content_encoding and content_encoding != "identity":
                    raise ProviderError("Source returned an encoded response")
                if response.status_code >= 400:
                    retry = response.headers.get("Retry-After", "")
                    retry_after: int | None = None
                    if retry.isdigit():
                        retry_after = int(retry)
                    elif retry:
                        # Retry-After dates are necessarily in the future, unlike event dates.
                        try:
                            parsed_retry = parsedate_to_datetime(retry).astimezone(UTC)
                            retry_after = max(0, int((parsed_retry - datetime.now(UTC)).total_seconds()))
                        except (TypeError, ValueError, OverflowError):
                            retry_after = None
                    raise ProviderError(f"Source returned HTTP {response.status_code}", retry_after=retry_after)
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > MAX_BODY:
                    raise ProviderError("Source response was too large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise ProviderError("Source response was too large")
                return bytes(body)
    except ProviderError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ProviderError("Source request timed out") from None
    except httpx.HTTPError:
        raise ProviderError("Source request failed") from None


async def _reddit_token(client: httpx.AsyncClient, config: WorldConfig) -> str:
    try:
        body = await _bounded_response(client, "POST", "https://www.reddit.com/api/v1/access_token", data={"grant_type": "refresh_token", "refresh_token": config.reddit_refresh_token.get_secret_value()}, auth=(config.reddit_client_id, config.reddit_client_secret.get_secret_value()), headers={"User-Agent": config.reddit_user_agent})
        payload = json.loads(body)
        token = payload.get("access_token", "") if isinstance(payload, dict) else ""
        # RFC 6750 section 2.1: validate the upstream credential before it can
        # become a header. Never stringify malformed JSON or echo it in errors.
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise ProviderError("Reddit authorization response was invalid")
        return token
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        raise ProviderError("Reddit authorization failed") from None


async def _fetch_source(source: Source, client: httpx.AsyncClient, config: WorldConfig, now: datetime) -> list[WorldEvent]:
    if not source_enabled(source, config):
        raise ProviderError(source_setup_note(source, config) or "Source is disabled")
    headers = {
        "Accept": "application/json" if source.kind in {"usgs_geojson", "gdelt", "x", "reddit"} else "application/rss+xml, application/atom+xml, application/xml, text/xml",
        "Accept-Encoding": "identity",
    }
    params: dict[str, str | int] | None = None
    if source.id == "sec":
        headers["User-Agent"] = config.sec_user_agent
    elif source.kind == "x":
        headers["Authorization"] = f"Bearer {config.x_bearer_token.get_secret_value()}"
        params = {"query": config.x_query.strip(), "max_results": 100, "tweet.fields": "created_at"}
    elif source.kind == "reddit":
        headers["User-Agent"] = config.reddit_user_agent
        headers["Authorization"] = f"Bearer {await _reddit_token(client, config)}"
        names = config.reddit_subreddits.replace(",", "+")
        request_url = f"https://oauth.reddit.com/r/{names}/new"
    else:
        request_url = source.url
    if source.kind != "reddit":
        request_url = source.url
    body = await _bounded_response(client, "GET", request_url, headers=headers, params=params)
    parser = parse_json if source.kind in {"usgs_geojson", "gdelt", "x", "reddit"} else parse_xml
    # Body, XML structure, record count and sanitizer input are bounded before
    # expensive work. Keep it off-loop, but retain the caller's concurrency slot
    # until the worker finishes: cancelling to_thread cannot stop its thread.
    task = asyncio.create_task(asyncio.to_thread(parser, source, body, now))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # Repeated shutdown/deadline cancellation cannot abandon work.
            except Exception:
                break
        await asyncio.gather(task, return_exceptions=True)
        raise


async def fetch_source(source: Source, client: httpx.AsyncClient, config: WorldConfig, now: datetime) -> list[WorldEvent]:
    try:
        async with asyncio.timeout(WHOLE_REQUEST_TIMEOUT):
            return await _fetch_source(source, client, config, now)
    except ProviderError:
        raise
    except TimeoutError:
        raise ProviderError("Source request timed out") from None
