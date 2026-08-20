"""Provider-neutral immutable contracts shared by analytical snapshots."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class FrozenContractBase(BaseModel):
    """Strict immutable base for versioned analytical wire contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunStage(str, Enum):
    QUEUED = "QUEUED"
    INGESTING = "INGESTING"
    RECONCILING = "RECONCILING"
    VALIDATING = "VALIDATING"
    MODELING = "MODELING"
    PUBLISHING = "PUBLISHING"


class RunOutcome(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SnapshotStatus(str, Enum):
    BLESSED = "BLESSED"
    DEGRADED = "DEGRADED"


class ActiveSnapshotFreshness(str, Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"


class GateStatus(str, Enum):
    PASSED = "PASSED"
    WARNED = "WARNED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


class RecoveryClass(str, Enum):
    USER_RESOLVABLE = "USER_RESOLVABLE"
    REFRESH_SOURCE_RESOLVABLE = "REFRESH_SOURCE_RESOLVABLE"
    MODEL_OWNER_UPDATE = "MODEL_OWNER_UPDATE"
    MIXED = "MIXED"


class GateEvidenceV1(FrozenContractBase):
    gate_code: str
    status: GateStatus
    recovery_class: RecoveryClass
    evidence: tuple[str, ...]
    recovery_action: str

    @field_validator("gate_code", "recovery_action")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gate code and recovery action must be nonblank")
        return value

    @field_validator("evidence")
    @classmethod
    def _evidence_is_specific(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("gate evidence must contain nonblank facts")
        return value


class ValuationCutV1(FrozenContractBase):
    target_cut_utc: datetime
    display_timezone: str
    capture_start_utc: datetime
    capture_end_utc: datetime

    @field_validator("target_cut_utc", "capture_start_utc", "capture_end_utc")
    @classmethod
    def _must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("valuation-cut timestamps must be explicitly UTC")
        return value

    @field_validator("display_timezone")
    @classmethod
    def _timezone_must_resolve(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("display timezone must resolve in the IANA database") from error
        return value

    @model_validator(mode="after")
    def _capture_window_is_ordered(self) -> "ValuationCutV1":
        if self.capture_start_utc > self.capture_end_utc:
            raise ValueError("capture start must not follow capture end")
        return self


def _canonicalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=False)
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical decimals must be finite")
        return "0" if value.is_zero() else format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical floats must be finite")
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be strings")
            canonical_key = unicodedata.normalize("NFC", key)
            if canonical_key in normalized:
                raise ValueError("mapping keys collide after NFC normalization")
            normalized[canonical_key] = _canonicalize(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value to the deterministic byte boundary hashed by T2."""

    canonical = _canonicalize(value)
    return json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
