"""Untrusted article links must have unambiguous DNS-hosted HTTP(S) origins."""
import pytest
from pydantic import ValidationError

from quantmind.world.models import WorldEvent
from quantmind.world.urls import canonicalize_public_http_url, validate_public_http_url


UNSAFE_LINKS = [
    "http://127.1:8000/api/health",
    "http://0177.0.0.1/a",
    "http://0x7f.0.0.1/a",
    "http://%31%32%37.0.0.1/a",
    "http://2130706433/a",
    "http://127.0.0.1/a",
    "http://[::1]/a",
    "http://[::ffff:127.0.0.1]/a",
    "http://192.168.1.1/a",
    "http://169.254.169.254/a",
    "http://8.8.8.8/a",  # Article links require DNS names, even for public IPs.
    "http://[2606:4700:4700::1111]/a",
    "http://localhost/a",
    "http://news.localhost./a",
    "http://news.local/a",
    "http://intranet/a",
    "https://@example.org/a",
    "https://user:password@example.org/a",
    "https://example.org\\@127.0.0.1/a",
    "https://example.org:99999/a",
    "https://bad_label.example.org/a",
    "http://１２７。０。０。１/a",
    "https://example.org/\narticle",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("url", UNSAFE_LINKS)
def test_rejects_ambiguous_or_non_dns_article_origins(url):
    with pytest.raises(ValueError, match="public HTTP"):
        validate_public_http_url(url)
    assert canonicalize_public_http_url(url) is None


@pytest.mark.parametrize("url", [
    "https://example.org/article?symbol=BP.L&source=rss#evidence",
    "https://www.ecb.europa.eu:443/press/政策.html",
    "https://xn--brse-5qa.de/news",
    "https://Example.ORG./article",
])
def test_preserves_valid_dns_article_links(url):
    assert validate_public_http_url(url) == url


@pytest.mark.parametrize("url", UNSAFE_LINKS)
def test_cached_event_contract_rejects_the_same_unsafe_links(url):
    with pytest.raises(ValidationError):
        WorldEvent.model_validate({
            "id": "cached", "source_id": "fixture", "source_name": "Fixture",
            "title": "Cached headline", "url": url, "summary": "",
            "published_at": "2026-09-06T09:00:00Z", "topics": [], "regions": [],
        })


def test_canonicalization_retains_evidence_query_and_removes_trackers():
    assert canonicalize_public_http_url(
        "https://example.org/article?symbol=BP.L&utm_source=feed&fbclid=tracking#top"
    ) == "https://example.org/article?symbol=BP.L"
