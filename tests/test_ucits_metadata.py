from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantmind.datastore.store import BarStore
from quantmind.instruments.metadata import (
    DistributionPolicy,
    MetadataProvenanceV1,
    ProfileFreshness,
    UcitsEtfProfileV1,
    UcitsProfileResolutionV1,
)
from quantmind.sources.providers.justetf import JustEtfProvider


FETCHED_AT = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
PROFILE_HTML = """
<html>
  <h1>iShares Core MSCI World UCITS ETF USD (Acc)</h1>
  <table>
    <tr><td data-testid="tl_etf-basics_value_isin">IE00B4L5Y983</td></tr>
    <tr><td data-testid="tl_etf-basics_value_fund-provider">BlackRock</td></tr>
    <tr><td data-testid="tl_etf-basics_value_fund-domicile">Ireland</td></tr>
    <tr><td data-testid="tl_etf-basics_value_total-expense-ratio">0.20% p.a.</td></tr>
    <tr><td data-testid="tl_etf-basics_value_distribution-policy">Accumulating</td></tr>
    <tr><td data-testid="tl_etf-basics_value_replication">Optimised sampling</td></tr>
    <tr><td data-testid="tl_etf-basics_value_index">MSCI World</td></tr>
  </table>
</html>
"""


def _profile(
    *,
    isin: str = "IE00B4L5Y983",
    fund_name: str = "iShares Core MSCI World UCITS ETF USD (Acc)",
    fetched_at: datetime = FETCHED_AT,
) -> UcitsEtfProfileV1:
    return UcitsEtfProfileV1(
        schema_version="ucits_etf_profile_v1",
        isin=isin,
        fund_name=fund_name,
        issuer="BlackRock",
        domicile="Ireland",
        ter_pct=Decimal("0.20"),
        distribution_policy=DistributionPolicy.ACCUMULATING,
        replication_method="Optimised sampling",
        benchmark_name="MSCI World",
        provenance=MetadataProvenanceV1(
            source="justetf",
            source_url=f"https://www.justetf.com/en/etf-profile.html?isin={isin.strip().upper()}",
            fetched_at_utc=fetched_at,
        ),
    )


def test_ucits_profile_normalizes_isin_identity():
    profile = _profile(isin=" ie00b4l5y983 ")

    assert profile.isin == "IE00B4L5Y983"


def test_ucits_profile_rejects_an_invalid_isin_checksum():
    profile = _profile()
    invalid = profile.model_dump()
    invalid["isin"] = "IE00B4L5Y984"
    with pytest.raises(ValidationError, match="checksum"):
        UcitsEtfProfileV1.model_validate(invalid)


def test_ucits_profile_rejects_a_non_string_isin_cleanly():
    invalid = _profile().model_dump()
    invalid["isin"] = 123456789012

    with pytest.raises(ValidationError, match="ISIN must be a string"):
        UcitsEtfProfileV1.model_validate(invalid)


def test_metadata_provenance_requires_an_https_url():
    with pytest.raises(ValidationError, match="HTTPS"):
        MetadataProvenanceV1(
            source="justetf",
            source_url="http://www.justetf.com/profile",
            fetched_at_utc=FETCHED_AT,
        )


def test_metadata_provenance_requires_a_utc_timestamp():
    with pytest.raises(ValidationError, match="UTC"):
        MetadataProvenanceV1(
            source="justetf",
            source_url="https://www.justetf.com/profile",
            fetched_at_utc=FETCHED_AT.replace(tzinfo=None),
        )


def test_stale_resolution_withholds_the_profile_but_preserves_last_provenance():
    provenance = MetadataProvenanceV1(
        source="justetf",
        source_url="https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
        fetched_at_utc=FETCHED_AT,
    )
    resolution = UcitsProfileResolutionV1(
        schema_version="ucits_profile_resolution_v1",
        isin="IE00B4L5Y983",
        freshness=ProfileFreshness.STALE,
        profile=None,
        last_successful_provenance=provenance,
        reason="cached profile expired and refresh failed",
    )

    assert resolution.profile is None
    assert resolution.last_successful_provenance == provenance

    invalid = resolution.model_dump()
    invalid["profile"] = {
        "schema_version": "ucits_etf_profile_v1",
        "isin": "IE00B4L5Y983",
        "fund_name": "iShares Core MSCI World UCITS ETF USD (Acc)",
        "issuer": "BlackRock",
        "domicile": "Ireland",
        "ter_pct": Decimal("0.20"),
        "distribution_policy": DistributionPolicy.ACCUMULATING,
        "replication_method": "Optimised sampling",
        "benchmark_name": "MSCI World",
        "provenance": provenance,
    }
    with pytest.raises(ValidationError, match="must not expose"):
        UcitsProfileResolutionV1.model_validate(invalid)


def test_fresh_resolution_requires_the_profile_provenance():
    with pytest.raises(ValidationError, match="fresh resolution provenance"):
        UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin="IE00B4L5Y983",
            freshness=ProfileFreshness.FRESH,
            profile=_profile(),
            last_successful_provenance=None,
            reason=None,
        )


def test_fresh_resolution_cannot_carry_a_failure_reason():
    profile = _profile()
    with pytest.raises(ValidationError, match="fresh resolution must not"):
        UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin=profile.isin,
            freshness=ProfileFreshness.FRESH,
            profile=profile,
            last_successful_provenance=profile.provenance,
            reason="refresh failed",
        )


def test_stale_resolution_provenance_must_name_the_requested_isin():
    with pytest.raises(ValidationError, match="stale resolution provenance"):
        UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin="IE00B4L5Y983",
            freshness=ProfileFreshness.STALE,
            profile=None,
            last_successful_provenance=MetadataProvenanceV1(
                source="justetf",
                source_url="https://www.justetf.com/en/etf-profile.html?isin=IE00BZ17CN18",
                fetched_at_utc=FETCHED_AT,
            ),
            reason="refresh failed",
        )


def test_missing_resolution_cannot_claim_last_successful_provenance():
    with pytest.raises(ValidationError, match="missing resolution"):
        UcitsProfileResolutionV1(
            schema_version="ucits_profile_resolution_v1",
            isin="IE00B4L5Y983",
            freshness=ProfileFreshness.MISSING,
            profile=None,
            last_successful_provenance=_profile().provenance,
            reason="cache missing",
        )


def test_bar_store_round_trips_profiles_by_normalized_isin(tmp_path):
    store = BarStore(tmp_path)
    profile = _profile()

    store.write_ucits_profile(profile)

    assert store.read_ucits_profile(" ie00b4l5y983 ") == profile


def test_bar_store_returns_none_for_a_missing_profile(tmp_path):
    store = BarStore(tmp_path)

    assert store.read_ucits_profile("IE00BZ17CN18") is None


def test_bar_store_rejects_corrupt_ucits_profile_instead_of_serving_it(tmp_path):
    store = BarStore(tmp_path)
    store.write_ucits_profile(_profile())
    (next((tmp_path / "ucits_profiles").glob("*.json"))).write_text("not json")

    with pytest.raises(ValueError, match="corrupt UCITS profile"):
        store.read_ucits_profile("IE00B4L5Y983")


def test_bar_store_rejects_profile_stored_under_a_different_isin(tmp_path):
    store = BarStore(tmp_path)
    requested_isin = "IE00B4L5Y983"
    other_profile = _profile(
        isin="IE00BZ17CN18",
        fund_name="iShares Core S&P 500 UCITS ETF USD (Acc)",
    )
    profile_path = tmp_path / "ucits_profiles" / f"{requested_isin}.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(other_profile.model_dump_json())

    with pytest.raises(ValueError, match="corrupt UCITS profile"):
        store.read_ucits_profile(requested_isin)


def test_justetf_fetch_parses_a_profile_and_publishes_it_by_isin(tmp_path):
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        return PROFILE_HTML

    store = BarStore(tmp_path)
    provider = JustEtfProvider(store, fetcher=fetcher)

    result = provider.resolve("ie00b4l5y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.FRESH
    assert result.reason is None
    assert result.profile == _profile()
    assert store.read_ucits_profile("IE00B4L5Y983") == result.profile
    assert calls == [
        "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"
    ]


def test_justetf_serves_a_fresh_cached_profile_without_network_access(tmp_path):
    store = BarStore(tmp_path)
    cached = _profile()
    store.write_ucits_profile(cached)

    def network_must_not_run(url: str) -> str:
        raise AssertionError(f"unexpected network request: {url}")

    provider = JustEtfProvider(store, fetcher=network_must_not_run)
    result = provider.resolve(
        "IE00B4L5Y983", now=FETCHED_AT + timedelta(days=29)
    )

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile == cached
    assert result.last_successful_provenance == cached.provenance


def test_expired_profile_is_withheld_when_refresh_fails(tmp_path):
    store = BarStore(tmp_path)
    cached = _profile()
    store.write_ucits_profile(cached)

    def unavailable(url: str) -> str:
        raise ConnectionError("network down")

    provider = JustEtfProvider(store, fetcher=unavailable)
    result = provider.resolve(
        "IE00B4L5Y983", now=FETCHED_AT + timedelta(days=31)
    )

    assert result.freshness is ProfileFreshness.STALE
    assert result.profile is None
    assert result.last_successful_provenance == cached.provenance
    assert "refresh failed" in result.reason
    assert store.read_ucits_profile("IE00B4L5Y983") == cached


def test_missing_profile_fails_closed_when_fetch_is_unavailable(tmp_path):
    def unavailable(url: str) -> str:
        raise TimeoutError("timed out")

    store = BarStore(tmp_path)
    result = JustEtfProvider(store, fetcher=unavailable).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert result.last_successful_provenance is None
    assert "fetch failed" in result.reason
    assert store.read_ucits_profile("IE00B4L5Y983") is None


def test_unrecognized_html_is_not_blessed_as_a_profile(tmp_path):
    consent_html = """
    <html>
      <h1>Privacy preferences</h1>
      <div data-testid="tl_etf-basics_value_tracking">accept cookies</div>
    </html>
    """
    store = BarStore(tmp_path)
    result = JustEtfProvider(store, fetcher=lambda url: consent_html).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse" in result.reason.lower()
    assert store.read_ucits_profile("IE00B4L5Y983") is None


def test_justetf_rejects_a_page_that_names_a_different_isin(tmp_path):
    wrong_profile_html = PROFILE_HTML.replace("IE00B4L5Y983", "IE00BZ17CN18")
    store = BarStore(tmp_path)

    result = JustEtfProvider(
        store, fetcher=lambda _url: wrong_profile_html
    ).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse" in result.reason.lower()
    assert store.read_ucits_profile("IE00B4L5Y983") is None


def test_justetf_rejects_a_default_fetch_redirect_to_a_different_isin(
    monkeypatch, tmp_path
):
    import io
    import quantmind.sources.providers.justetf as justetf

    class RedirectedResponse:
        headers = {}

        def __init__(self):
            self._body = io.BytesIO(PROFILE_HTML.encode())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return self._body.read(limit)

        def geturl(self):
            return "https://www.justetf.com/en/etf-profile.html?isin=IE00BZ17CN18"

    class Opener:
        def open(self, *_args, **_kwargs):
            return RedirectedResponse()

    monkeypatch.setattr(justetf, "build_opener", lambda *_handlers: Opener())
    store = BarStore(tmp_path)

    result = JustEtfProvider(store).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse" in result.reason.lower()
    assert store.read_ucits_profile("IE00B4L5Y983") is None


@pytest.mark.parametrize(
    "redirect_url",
    [
        "http://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
        "https://evil.example/en/etf-profile.html?isin=IE00B4L5Y983",
        "https://127.0.0.1/internal",
    ],
)
def test_justetf_redirect_handler_rejects_untrusted_targets(redirect_url):
    import quantmind.sources.providers.justetf as justetf

    handler = justetf._JustEtfRedirectHandler()

    with pytest.raises(ValueError, match="HTTPS justETF URL"):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            redirect_url,
        )


def test_justetf_redirect_handler_allows_an_https_justetf_target():
    from urllib.request import Request
    import quantmind.sources.providers.justetf as justetf

    redirected = justetf._JustEtfRedirectHandler().redirect_request(
        Request("https://justetf.com/en/etf-profile.html"),
        None,
        302,
        "Found",
        {},
        "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
    )

    assert redirected.full_url == (
        "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"
    )


def test_justetf_validates_the_final_url_before_reading_the_body(monkeypatch):
    import io
    import quantmind.sources.providers.justetf as justetf

    class RedirectedResponse:
        headers = {}

        def __init__(self):
            self._body = io.BytesIO(PROFILE_HTML.encode())
            self.read_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            self.read_calls += 1
            return self._body.read(limit)

        def geturl(self):
            return "https://evil.example/private"

    response = RedirectedResponse()

    class Opener:
        def open(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(justetf, "build_opener", lambda *_handlers: Opener())

    with pytest.raises(ValueError, match="HTTPS justETF URL"):
        justetf._default_fetcher(
            "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"
        )

    assert response.read_calls == 0


def test_justetf_identifies_quantmind_in_its_user_agent(monkeypatch):
    import io
    import quantmind.sources.providers.justetf as justetf

    class Response:
        headers = {}

        def __init__(self):
            self._body = io.BytesIO(PROFILE_HTML.encode())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return self._body.read(limit)

        def geturl(self):
            return "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"

    class Opener:
        def open(self, request, **_kwargs):
            assert request.get_header("User-agent") == (
                "QuantMind/0.5.0.0 (+https://github.com/ionutnodis/quant-mind)"
            )
            return Response()

    monkeypatch.setattr(justetf, "build_opener", lambda *_handlers: Opener())

    fetched = justetf._default_fetcher(
        "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"
    )

    assert fetched.html == PROFILE_HTML


def test_corrupt_cached_profile_is_replaced_after_a_successful_refetch(tmp_path):
    store = BarStore(tmp_path)
    store.write_ucits_profile(_profile())
    next((tmp_path / "ucits_profiles").glob("*.json")).write_text("not json")

    result = JustEtfProvider(store, fetcher=lambda url: PROFILE_HTML).resolve(
        "IE00B4L5Y983", now=FETCHED_AT + timedelta(days=1)
    )

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile == _profile(fetched_at=FETCHED_AT + timedelta(days=1))
    assert store.read_ucits_profile("IE00B4L5Y983") == result.profile


def test_corrupt_cache_and_failed_refetch_reports_missing_with_corruption(tmp_path):
    store = BarStore(tmp_path)
    store.write_ucits_profile(_profile())
    next((tmp_path / "ucits_profiles").glob("*.json")).write_text("not json")

    def unavailable(url: str) -> str:
        raise ConnectionError("network down")

    result = JustEtfProvider(store, fetcher=unavailable).resolve(
        "IE00B4L5Y983", now=FETCHED_AT + timedelta(days=1)
    )

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert result.last_successful_provenance is None
    assert "corrupt cache" in result.reason


def test_future_dated_cache_is_not_treated_as_fresh(tmp_path):
    store = BarStore(tmp_path)
    future = _profile(fetched_at=FETCHED_AT + timedelta(days=1))
    store.write_ucits_profile(future)

    result = JustEtfProvider(store, fetcher=lambda url: PROFILE_HTML).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile.provenance.fetched_at_utc == FETCHED_AT
    assert store.read_ucits_profile("IE00B4L5Y983") == result.profile


def test_profile_provenance_url_must_name_the_same_isin():
    invalid = _profile().model_dump()
    invalid["provenance"] = {
        "source": "justetf",
        "source_url": "https://www.justetf.com/en/etf-profile.html?isin=IE00BZ17CN18",
        "fetched_at_utc": FETCHED_AT,
    }

    with pytest.raises(ValidationError, match="same ISIN"):
        UcitsEtfProfileV1.model_validate(invalid)


def test_justetf_provenance_rejects_a_different_host():
    with pytest.raises(ValidationError, match="justetf.com"):
        MetadataProvenanceV1(
            source="justetf",
            source_url="https://example.com/en/etf-profile.html?isin=IE00B4L5Y983",
            fetched_at_utc=FETCHED_AT,
        )


@pytest.mark.parametrize(
    "ter",
    [Decimal("-0.01"), Decimal("5.01"), Decimal("NaN"), Decimal("Infinity")],
)
def test_profile_rejects_invalid_expense_ratios(ter):
    invalid = _profile().model_dump()
    invalid["ter_pct"] = ter

    with pytest.raises(ValidationError, match="expense ratio"):
        UcitsEtfProfileV1.model_validate(invalid)


def test_optional_text_facts_normalize_blank_values_to_unknown():
    payload = _profile().model_dump()
    payload.update(
        issuer=" ",
        domicile="\n",
        replication_method="",
        benchmark_name="\t",
    )

    profile = UcitsEtfProfileV1.model_validate(payload)

    assert profile.issuer is None
    assert profile.domicile is None
    assert profile.replication_method is None
    assert profile.benchmark_name is None


def test_justetf_distinguishes_distributing_share_classes(tmp_path):
    html = PROFILE_HTML.replace("Accumulating", "Distributing")

    result = JustEtfProvider(BarStore(tmp_path), fetcher=lambda url: html).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.profile.distribution_policy is DistributionPolicy.DISTRIBUTING


def test_justetf_parser_supports_the_live_profile_field_identifiers(tmp_path):
    live_shape = """
    <html><h1>iShares Core MSCI World UCITS ETF USD (Acc)</h1>
    <table>
      <tr><td data-testid="tl_etf-basics_value_isin">IE00B4L5Y983</td></tr>
      <tr><td data-testid="tl_etf-basics_value_index-name">MSCI World</td></tr>
      <tr><td data-testid="tl_etf-basics_value_ter">0.20% p.a.</td></tr>
      <tr><td data-testid="tl_etf-basics_value_replication">Physical</td></tr>
      <tr><td data-testid="tl_etf-basics_value_replication-method">Optimized sampling</td></tr>
      <tr><td data-testid="tl_etf-basics_value_distribution-policy">Accumulating</td></tr>
      <tr><td data-testid="tl_etf-basics_value_domicile-country">Ireland</td></tr>
      <tr><td data-testid="tl_etf-basics_value_fund-provider">iShares</td></tr>
    </table></html>
    """

    result = JustEtfProvider(
        BarStore(tmp_path), fetcher=lambda _url: live_shape
    ).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile.ter_pct == Decimal("0.20")
    assert result.profile.domicile == "Ireland"
    assert result.profile.replication_method == "Physical · Optimized sampling"
    assert result.profile.benchmark_name == "MSCI World"


def test_justetf_parser_handles_nested_cells_attributes_and_comma_ter(tmp_path):
    live_shape = """
    <html><h1><span>iShares Core MSCI World</span> UCITS ETF</h1><table>
      <tr><td data-testid="tl_etf-basics_value_isin">IE00B4L5Y983</td></tr>
      <tr><td data-testid="tl_etf-basics_value_fund-domicile"><img class="flag">Ireland</td></tr>
      <tr><td data-testid="tl_etf-basics_value_ter" data-tooltip="charges > 0">0,20% p.a.</td></tr>
    </table></html>
    """

    result = JustEtfProvider(
        BarStore(tmp_path), fetcher=lambda _url: live_shape
    ).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile.fund_name == "iShares Core MSCI World UCITS ETF"
    assert result.profile.domicile == "Ireland"
    assert result.profile.ter_pct == Decimal("0.20")


def test_justetf_parser_rejects_context_date_as_an_expense_ratio(tmp_path):
    html = PROFILE_HTML.replace("0.20% p.a.", "as of 31.12.2025: 0.20%")

    result = JustEtfProvider(BarStore(tmp_path), fetcher=lambda _url: html).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse failed" in result.reason


def test_justetf_parser_requires_a_structured_matching_isin(tmp_path):
    soft_not_found = """
    <html><h1>IE00B4L5Y983 was not found</h1><table>
      <tr><td data-testid="tl_etf-basics_value_ter">0.20%</td></tr>
    </table></html>
    """

    result = JustEtfProvider(
        BarStore(tmp_path), fetcher=lambda _url: soft_not_found
    ).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse failed" in result.reason


def test_justetf_parser_rejects_profiles_with_only_empty_fact_cells(tmp_path):
    empty_profile = """
    <html><h1>Placeholder fund</h1><table>
      <tr><td data-testid="tl_etf-basics_value_isin">IE00B4L5Y983</td></tr>
      <tr><td data-testid="tl_etf-basics_value_fund-provider"><span></span></td></tr>
    </table></html>
    """

    result = JustEtfProvider(
        BarStore(tmp_path), fetcher=lambda _url: empty_profile
    ).resolve("IE00B4L5Y983", now=FETCHED_AT)

    assert result.freshness is ProfileFreshness.MISSING
    assert result.profile is None
    assert "parse failed" in result.reason


def test_justetf_provider_has_a_default_fetch_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "quantmind.sources.providers.justetf._default_fetcher",
        lambda url: PROFILE_HTML,
    )

    result = JustEtfProvider(BarStore(tmp_path)).resolve(
        "IE00B4L5Y983", now=FETCHED_AT
    )

    assert result.freshness is ProfileFreshness.FRESH
    assert result.profile.isin == "IE00B4L5Y983"
