"""Fail-closed justETF profile ingestion for ISIN-addressed UCITS ETFs."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from quantmind.instruments.metadata import (
    DistributionPolicy,
    MetadataProvenanceV1,
    ProfileFreshness,
    UCITS_PROFILE_MAX_AGE_DAYS,
    UcitsEtfProfileV1,
    UcitsProfileResolutionV1,
    is_ucits_profile_fresh,
    normalize_isin,
)
from quantmind.sources.http import read_bounded_text


_TEST_ID_PREFIX = "tl_etf-basics_value_"
_TER_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*%")
DEFAULT_MAX_AGE_DAYS = UCITS_PROFILE_MAX_AGE_DAYS
_PROFILE_FIELDS = frozenset(
    {
        "distribution-policy",
        "fund-provider",
        "fund-domicile",
        "domicile-country",
        "total-expense-ratio",
        "ter",
        "replication",
        "replication-method",
        "index",
        "index-name",
    }
)
_USER_AGENT = "QuantMind/0.5.0.0 (+https://github.com/ionutnodis/quant-mind)"
_MAX_PROFILE_RESPONSE_BYTES = 5 * 1024 * 1024
_ALLOWED_JUSTETF_HOSTS = frozenset({"justetf.com", "www.justetf.com"})
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


class UcitsProfileStore(Protocol):
    def read_ucits_profile(self, isin: str) -> UcitsEtfProfileV1 | None: ...

    def write_ucits_profile(self, profile: UcitsEtfProfileV1) -> None: ...


@dataclass(frozen=True)
class _FetchedProfilePage:
    html: str
    final_url: str


def profile_url(isin: str) -> str:
    return f"https://www.justetf.com/en/etf-profile.html?isin={normalize_isin(isin)}"


def _require_justetf_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_JUSTETF_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError(
            "justETF requests and redirects require an HTTPS justETF URL"
        )
    return url


class _JustEtfRedirectHandler(HTTPRedirectHandler):
    """Follow redirects only while every hop remains on the justETF allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_justetf_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_fetcher(url: str) -> _FetchedProfilePage:
    _require_justetf_url(url)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    context = ssl.create_default_context(cafile=certifi.where())
    opener = build_opener(
        HTTPSHandler(context=context),
        _JustEtfRedirectHandler(),
    )
    with opener.open(request, timeout=20) as response:
        # Check the resolved destination before reading even one byte.  The
        # redirect handler validates normal hops; this second gate also keeps
        # a surprising/custom response implementation fail-closed.
        final_url = _require_justetf_url(response.geturl())
        html = read_bounded_text(
            response,
            max_bytes=_MAX_PROFILE_RESPONSE_BYTES,
            encoding="utf-8",
            errors="replace",
        )
        return _FetchedProfilePage(html=html, final_url=final_url)


def _text(value: str) -> str:
    return " ".join(value.split())


class _ProfileHtmlParser(HTMLParser):
    """Extract named facts without treating ``>`` inside attributes as markup.

    justETF value cells commonly contain nested spans (for example a country
    flag). A regex over raw HTML can either truncate at the nested closing tag
    or terminate an opening tag at a quoted ``>``. ``HTMLParser`` gives us the
    actual attribute and element boundaries while keeping this adapter free of
    a heavyweight DOM dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: dict[str, str] = {}
        self.fund_name: str | None = None
        self._open_tags: list[str] = []
        self._cell: tuple[str, str, int, list[str]] | None = None
        self._heading: tuple[int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _VOID_HTML_TAGS:
            return
        self._open_tags.append(normalized_tag)
        depth = len(self._open_tags)
        if normalized_tag == "h1" and self._heading is None and self.fund_name is None:
            self._heading = (depth, [])
        if self._cell is not None or normalized_tag not in {"td", "div", "span"}:
            return
        attributes = {key.lower(): value for key, value in attrs}
        test_id = attributes.get("data-testid") or ""
        if test_id.lower().startswith(_TEST_ID_PREFIX):
            key = test_id[len(_TEST_ID_PREFIX) :].strip().lower()
            if key:
                self._cell = (key, normalized_tag, depth, [])

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        # A self-closing element cannot carry a usable text fact. It may sit
        # inside an active cell (for example an icon), where it is ignored.
        return

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell[3].append(data)
        if self._heading is not None:
            self._heading[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        depth = len(self._open_tags)
        if self._cell is not None:
            key, container_tag, start_depth, parts = self._cell
            if normalized_tag == container_tag and depth == start_depth:
                value = _text(" ".join(parts))
                existing = self.cells.get(key)
                if existing and value and existing != value:
                    raise ValueError(f"conflicting justETF values for {key}")
                if key not in self.cells or value:
                    self.cells[key] = value
                self._cell = None
        if self._heading is not None:
            start_depth, parts = self._heading
            if normalized_tag == "h1" and depth == start_depth:
                self.fund_name = _text(" ".join(parts)) or None
                self._heading = None
        if self._open_tags and self._open_tags[-1] == normalized_tag:
            self._open_tags.pop()


def _profile_fields(html: str) -> tuple[dict[str, str], str | None]:
    parser = _ProfileHtmlParser()
    parser.feed(html)
    parser.close()
    return parser.cells, parser.fund_name


def _parse_profile(
    html: str,
    isin: str,
    fetched_at: datetime,
    *,
    source_url: str,
) -> UcitsEtfProfileV1:
    cells, fund_name = _profile_fields(html)
    structured_isin = cells.get("isin")
    if structured_isin is None:
        raise ValueError("justETF page does not identify the requested ISIN")
    try:
        page_isin = normalize_isin(structured_isin)
    except (TypeError, ValueError) as exc:
        raise ValueError("justETF page contains an invalid structured ISIN") from exc
    if page_isin != isin:
        raise ValueError("justETF page does not identify the requested ISIN")
    policy_text = cells.get("distribution-policy", "").lower()
    policy = {
        "accumulating": DistributionPolicy.ACCUMULATING,
        "distributing": DistributionPolicy.DISTRIBUTING,
    }.get(policy_text, DistributionPolicy.UNKNOWN)
    ter_pct = None
    ter_text = cells.get("total-expense-ratio") or cells.get("ter", "")
    if ter_text:
        match = _TER_RE.match(ter_text)
        if match is None:
            raise ValueError("justETF expense ratio is unparseable")
        try:
            ter_pct = Decimal(match.group(1).replace(",", "."))
        except InvalidOperation:
            raise ValueError("justETF expense ratio is unparseable") from None
    if not fund_name or not any(cells.get(key) for key in _PROFILE_FIELDS):
        raise ValueError("justETF profile is unparseable")
    provenance = MetadataProvenanceV1(
        source="justetf", source_url=source_url, fetched_at_utc=fetched_at
    )
    replication_parts = list(
        dict.fromkeys(
            part
            for part in (cells.get("replication"), cells.get("replication-method"))
            if part
        )
    )
    return UcitsEtfProfileV1(
        schema_version="ucits_etf_profile_v1",
        isin=isin,
        fund_name=fund_name,
        issuer=cells.get("fund-provider") or None,
        domicile=cells.get("fund-domicile") or cells.get("domicile-country") or None,
        ter_pct=ter_pct,
        distribution_policy=policy,
        replication_method=" · ".join(replication_parts) or None,
        benchmark_name=cells.get("index") or cells.get("index-name") or None,
        provenance=provenance,
    )


def _failed_resolution(
    *,
    isin: str,
    cached: UcitsEtfProfileV1 | None,
    cache_was_corrupt: bool,
    stage: str,
    error: Exception,
) -> UcitsProfileResolutionV1:
    operation = (
        "refresh" if cached is not None and stage == "fetch"
        else f"refresh {stage}" if cached is not None
        else stage
    )
    reason = f"justETF {operation} failed ({type(error).__name__})"
    if cached is not None:
        return UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin=isin,
            freshness=ProfileFreshness.STALE,
            profile=None,
            last_successful_provenance=cached.provenance,
            reason=reason,
        )
    return UcitsProfileResolutionV1(
        schema_version="ucits_profile_resolution_v1",
        isin=isin,
        freshness=ProfileFreshness.MISSING,
        profile=None,
        last_successful_provenance=None,
        reason=("corrupt cache; " if cache_was_corrupt else "") + reason,
    )


class JustEtfProvider:
    """Resolve a justETF profile through an injected network fetcher."""

    def __init__(
        self,
        store: UcitsProfileStore,
        *,
        fetcher: Callable[[str], str | _FetchedProfilePage] | None = None,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    ):
        self._store = store
        self._fetcher = fetcher or _default_fetcher
        self._max_age = timedelta(days=max_age_days)

    def resolve(self, isin: str, *, now: datetime) -> UcitsProfileResolutionV1:
        normalized = normalize_isin(isin)
        cache_was_corrupt = False
        try:
            cached = self._store.read_ucits_profile(normalized)
        except ValueError:
            cached = None
            cache_was_corrupt = True
        if cached is not None and is_ucits_profile_fresh(
            cached,
            now=now,
            max_age_days=self._max_age.total_seconds() / 86_400,
        ):
            return UcitsProfileResolutionV1(
                schema_version="ucits_profile_resolution_v1",
                isin=normalized,
                freshness=ProfileFreshness.FRESH,
                profile=cached,
                last_successful_provenance=cached.provenance,
                reason=None,
            )
        try:
            requested_url = profile_url(normalized)
            fetched = self._fetcher(requested_url)
            if isinstance(fetched, str):
                page = _FetchedProfilePage(html=fetched, final_url=requested_url)
            elif isinstance(fetched, _FetchedProfilePage):
                page = fetched
            else:
                raise TypeError("justETF fetcher returned an unsupported response")
        except Exception as error:
            return _failed_resolution(
                isin=normalized,
                cached=cached,
                cache_was_corrupt=cache_was_corrupt,
                stage="fetch",
                error=error,
            )
        try:
            profile = _parse_profile(
                page.html,
                normalized,
                now,
                source_url=page.final_url,
            )
        except Exception as error:
            return _failed_resolution(
                isin=normalized,
                cached=cached,
                cache_was_corrupt=cache_was_corrupt,
                stage="parse",
                error=error,
            )
        self._store.write_ucits_profile(profile)
        return UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin=normalized,
            freshness=ProfileFreshness.FRESH,
            profile=profile,
            last_successful_provenance=profile.provenance,
            reason=None,
        )
