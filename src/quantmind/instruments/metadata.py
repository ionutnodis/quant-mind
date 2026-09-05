"""Typed metadata for ISIN-addressed UCITS ETF share classes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationInfo, field_validator, model_validator

from quantmind.snapshots.contracts import FrozenContractBase


_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_SUPPORTED_EUROPEAN_FUND_DOMICILES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GR",
        "HR",
        "HU",
        "IE",
        "IS",
        "IT",
        "LI",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)
UCITS_PROFILE_MAX_AGE_DAYS = 30.0


def is_potential_ucits_isin(value: object) -> bool:
    """Return whether an identifier has a supported European domicile prefix.

    This is a conservative ingestion-routing hint, not a regulatory claim that
    the instrument is UCITS compliant.  Full checksum validation remains a
    separate requirement before any provider request is made.
    """

    return (
        isinstance(value, str)
        and len(value.strip()) >= 2
        and value.strip().upper()[:2] in _SUPPORTED_EUROPEAN_FUND_DOMICILES
    )


def normalize_isin(value: str) -> str:
    """Return a canonical ISO 6166 identifier after validating its checksum."""
    if not isinstance(value, str):
        raise ValueError("ISIN must be a string")
    isin = value.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise ValueError("ISIN must be 12 uppercase alphanumeric characters")
    digits = "".join(str(int(character, 36)) for character in isin)
    checksum = 0
    for index, character in enumerate(reversed(digits)):
        product = int(character) * (2 if index % 2 else 1)
        tens, units = divmod(product, 10)
        checksum += tens + units
    if checksum % 10:
        raise ValueError("ISIN checksum is invalid")
    return isin


def _profile_url_isin(value: str) -> str | None:
    candidates = parse_qs(urlparse(value).query).get("isin", [])
    if len(candidates) != 1:
        return None
    try:
        return normalize_isin(candidates[0])
    except (AttributeError, TypeError, ValueError):
        return None


class DistributionPolicy(str, Enum):
    ACCUMULATING = "ACCUMULATING"
    DISTRIBUTING = "DISTRIBUTING"
    UNKNOWN = "UNKNOWN"


class ProfileFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


class MetadataProvenanceV1(FrozenContractBase):
    source: Literal["justetf"]
    source_url: str
    fetched_at_utc: datetime

    @field_validator("source_url")
    @classmethod
    def _source_url_is_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("metadata source URL must use HTTPS")
        if parsed.hostname not in {"justetf.com", "www.justetf.com"}:
            raise ValueError("justETF provenance must use justetf.com")
        return value

    @field_validator("fetched_at_utc")
    @classmethod
    def _fetched_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("metadata fetch timestamp must be explicitly UTC")
        return value


class UcitsEtfProfileV1(FrozenContractBase):
    schema_version: Literal["ucits_etf_profile_v1"]
    isin: str
    fund_name: str | None
    issuer: str | None
    domicile: str | None
    ter_pct: Decimal | None
    distribution_policy: DistributionPolicy
    replication_method: str | None
    benchmark_name: str | None
    provenance: MetadataProvenanceV1

    @field_validator("isin", mode="before")
    @classmethod
    def _isin_is_canonical(cls, value: str) -> str:
        return normalize_isin(value)

    @field_validator("ter_pct", mode="before")
    @classmethod
    def _expense_ratio_is_valid(
        cls, value: Decimal | str | None, info: ValidationInfo
    ) -> Decimal | str | None:
        candidate = (
            Decimal(value) if info.mode == "json" and isinstance(value, str) else value
        )
        if isinstance(candidate, Decimal) and (
            not candidate.is_finite() or candidate < 0 or candidate > Decimal("5")
        ):
            raise ValueError(
                "expense ratio must be finite and between 0 and 5 percent"
            )
        return candidate

    @field_validator(
        "fund_name",
        "issuer",
        "domicile",
        "replication_method",
        "benchmark_name",
        mode="before",
    )
    @classmethod
    def _blank_fact_is_unknown(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def _provenance_names_the_profile(self) -> "UcitsEtfProfileV1":
        if _profile_url_isin(self.provenance.source_url) != self.isin:
            raise ValueError("profile provenance URL must name the same ISIN")
        return self


def is_ucits_profile_fresh(
    profile: UcitsEtfProfileV1,
    *,
    now: datetime,
    max_age_days: float = UCITS_PROFILE_MAX_AGE_DAYS,
) -> bool:
    """Evaluate the cache TTL at the read boundary, including clock skew."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("profile freshness requires a timezone-aware timestamp")
    fetched_at = profile.provenance.fetched_at_utc.astimezone(timezone.utc)
    age = now.astimezone(timezone.utc) - fetched_at
    return timedelta(0) <= age <= timedelta(days=max_age_days)


class UcitsProfileResolutionV1(FrozenContractBase):
    schema_version: Literal["ucits_profile_resolution_v1"]
    isin: str
    freshness: ProfileFreshness
    profile: UcitsEtfProfileV1 | None
    last_successful_provenance: MetadataProvenanceV1 | None
    reason: str | None

    @field_validator("isin", mode="before")
    @classmethod
    def _isin_is_canonical(cls, value: str) -> str:
        return normalize_isin(value)

    @model_validator(mode="after")
    def _shape_matches_freshness(self) -> "UcitsProfileResolutionV1":
        if self.freshness is ProfileFreshness.FRESH:
            if self.profile is None or self.profile.isin != self.isin:
                raise ValueError("fresh resolution requires a matching profile")
            if self.last_successful_provenance != self.profile.provenance:
                raise ValueError("fresh resolution provenance must match its profile")
            if self.reason is not None:
                raise ValueError("fresh resolution must not carry a failure reason")
        elif self.profile is not None:
            raise ValueError("stale or missing resolution must not expose a profile")
        if (
            self.freshness is ProfileFreshness.STALE
            and self.last_successful_provenance is None
        ):
            raise ValueError("stale resolution requires last successful provenance")
        if self.freshness is ProfileFreshness.STALE:
            if _profile_url_isin(self.last_successful_provenance.source_url) != self.isin:
                raise ValueError("stale resolution provenance must name its ISIN")
        if (
            self.freshness is ProfileFreshness.MISSING
            and self.last_successful_provenance is not None
        ):
            raise ValueError("missing resolution must not claim successful provenance")
        if self.freshness is not ProfileFreshness.FRESH and not (self.reason or "").strip():
            raise ValueError("stale or missing resolution requires a reason")
        return self
