"""Honest read-only references for frozen 12-character legacy book snapshots."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from quantmind.core.snapshot import BookSnapshot
from quantmind.snapshots.contracts import FrozenContractBase
from quantmind.snapshots.input_artifacts import ReproducibilityClass
from quantmind.snapshots.manifest import ManifestError, load_unambiguous_json_bytes


_LEGACY_BOOK_REF_RE = re.compile(r"^[0-9a-f]{12}$")
_FULL_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_BASE_LIMITATIONS = (
    "MISSING_COMPLETE_MARKS",
    "MISSING_FX_OBSERVATIONS",
    "MISSING_INCLUDED_ACCOUNTS",
    "MISSING_NET_LIQUIDATION_VALUE",
    "MISSING_RIGHTS_PROVENANCE",
    "MISSING_VALUATION_CUT_EVIDENCE",
    "MISSING_MODEL_DATA_CODE_VERSIONS",
)


class LegacyBookError(ValueError):
    pass


class InvalidLegacyBookRefError(LegacyBookError):
    pass


class LegacyBookNotFoundError(LegacyBookError):
    pass


class LegacyBookCorruptError(LegacyBookError):
    pass


class NonRegularLegacyBookFileError(LegacyBookCorruptError):
    pass


def _validate_book_ref(value: str) -> str:
    if not isinstance(value, str) or not _LEGACY_BOOK_REF_RE.fullmatch(value):
        raise InvalidLegacyBookRefError(
            "legacy book ref must be 12 lowercase hexadecimal characters"
        )
    return value


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    return value


def _validate_valuation_timestamp(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("legacy valuation timestamp must be UTC and Z-suffixed")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError("legacy valuation timestamp is invalid") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("legacy valuation timestamp must be UTC")
    return value


def _validate_currency(value: str) -> str:
    if len(value) != 3 or any(character < "A" or character > "Z" for character in value):
        raise ValueError("legacy base currency must be an uppercase three-letter code")
    return value


class LegacyBookPositionV0(FrozenContractBase):
    """Exact position shape written by the existing legacy book route."""

    con_id: int | None
    symbol: str
    qty: Decimal
    sec_type: str
    multiplier: Decimal
    strike: Decimal | None
    expiry: str | None
    right: Literal["C", "P"] | None

    @field_validator("symbol", "sec_type")
    @classmethod
    def _strings_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("qty", "multiplier", "strike")
    @classmethod
    def _numbers_are_finite(cls, value: Decimal | None, info) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @model_validator(mode="after")
    def _legacy_position_shape_is_noninvented(self) -> "LegacyBookPositionV0":
        if self.multiplier <= 0:
            raise ValueError("legacy position multiplier must be positive")
        if self.strike is not None and self.strike <= 0:
            raise ValueError("legacy option strike must be positive")
        if self.sec_type != "OPT" and any(
            value is not None for value in (self.strike, self.expiry, self.right)
        ):
            raise ValueError("non-option legacy positions cannot carry option terms")
        if self.expiry is not None and not self.expiry.strip():
            raise ValueError("legacy option expiry must be nonblank when present")
        return self


class LegacyBookPayloadV0(FrozenContractBase):
    snapshot_id: str
    valuation_ts: str
    base_currency: str
    positions: tuple[LegacyBookPositionV0, ...]

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_is_legacy_shape(cls, value: str) -> str:
        return _validate_book_ref(value)

    @field_validator("valuation_ts")
    @classmethod
    def _valuation_is_utc(cls, value: str) -> str:
        return _validate_valuation_timestamp(value)

    @field_validator("base_currency")
    @classmethod
    def _currency_is_explicit(cls, value: str) -> str:
        return _validate_currency(value)


class LegacyPositionSummaryV1(FrozenContractBase):
    con_id: int | None
    symbol: str
    quantity: Decimal
    multiplier: Decimal
    security_type: str
    option_strike: Decimal | None
    option_expiry: str | None
    option_right: Literal["C", "P"] | None

    @field_validator("symbol", "security_type")
    @classmethod
    def _strings_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("quantity", "multiplier", "option_strike")
    @classmethod
    def _numbers_are_finite(cls, value: Decimal | None, info) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @model_validator(mode="after")
    def _summary_shape_is_consistent(self) -> "LegacyPositionSummaryV1":
        if self.multiplier <= 0:
            raise ValueError("legacy position multiplier must be positive")
        if self.security_type != "OPT" and any(
            value is not None
            for value in (self.option_strike, self.option_expiry, self.option_right)
        ):
            raise ValueError("non-option legacy position cannot carry option terms")
        return self


class LegacyBookReferenceV1(FrozenContractBase):
    schema_version: Literal["legacy_book_reference_v1"]
    book_ref: str
    valuation_ts: str
    base_currency: str
    position_count: int = Field(ge=0)
    positions: tuple[LegacyPositionSummaryV1, ...]
    legacy_content_sha256: str | None
    reproducibility_class: ReproducibilityClass
    limitations: tuple[str, ...]
    refused_outputs: tuple[str, ...]

    @field_validator("book_ref")
    @classmethod
    def _book_ref_is_legacy_shape(cls, value: str) -> str:
        return _validate_book_ref(value)

    @field_validator("valuation_ts")
    @classmethod
    def _valuation_is_utc(cls, value: str) -> str:
        return _validate_valuation_timestamp(value)

    @field_validator("base_currency")
    @classmethod
    def _currency_is_explicit(cls, value: str) -> str:
        return _validate_currency(value)

    @field_validator("legacy_content_sha256")
    @classmethod
    def _optional_content_hash_is_full(cls, value: str | None) -> str | None:
        if value is not None and not _FULL_DIGEST_RE.fullmatch(value):
            raise ValueError("legacy content hash must be a full lowercase SHA-256")
        return value

    @field_validator("limitations", "refused_outputs")
    @classmethod
    def _evidence_is_sorted_unique(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        if not values or any(not value.strip() for value in values):
            raise ValueError(f"{info.field_name} must contain nonblank evidence")
        if values != tuple(sorted(set(values))):
            raise ValueError(f"{info.field_name} must be sorted and unique")
        return values

    @model_validator(mode="after")
    def _reference_is_honest(self) -> "LegacyBookReferenceV1":
        if self.position_count != len(self.positions):
            raise ValueError("legacy position count is inconsistent")
        if self.reproducibility_class is not ReproducibilityClass.NON_REPRODUCIBLE_LEGACY:
            raise ValueError("legacy books must remain NON_REPRODUCIBLE_LEGACY")
        return self


def _build_reference(
    *,
    book_ref: str,
    valuation_ts: str,
    base_currency: str,
    positions: tuple[LegacyPositionSummaryV1, ...],
    content_sha256: str | None,
) -> LegacyBookReferenceV1:
    incomplete_option_terms = any(
        position.security_type == "OPT"
        and any(
            value is None
            for value in (
                position.option_strike,
                position.option_expiry,
                position.option_right,
            )
        )
        for position in positions
    )
    limitations = set(_BASE_LIMITATIONS)
    refused_outputs = {"ANALYTICAL_SNAPSHOT_PUBLICATION"}
    if incomplete_option_terms:
        limitations.add("MISSING_COMPLETE_OPTION_TERMS")
        refused_outputs.add("OPTION_REPRICING")
    return LegacyBookReferenceV1(
        schema_version="legacy_book_reference_v1",
        book_ref=book_ref,
        valuation_ts=valuation_ts,
        base_currency=base_currency,
        position_count=len(positions),
        positions=positions,
        legacy_content_sha256=content_sha256,
        reproducibility_class=ReproducibilityClass.NON_REPRODUCIBLE_LEGACY,
        limitations=tuple(sorted(limitations)),
        refused_outputs=tuple(sorted(refused_outputs)),
    )


def adapt_legacy_book_snapshot(snapshot: BookSnapshot) -> LegacyBookReferenceV1:
    if not isinstance(snapshot, BookSnapshot):
        raise TypeError("legacy adapter requires BookSnapshot")
    positions = tuple(
        LegacyPositionSummaryV1(
            con_id=position.con_id,
            symbol=position.symbol,
            quantity=Decimal(str(position.qty)),
            multiplier=Decimal(str(position.multiplier)),
            security_type=position.sec_type,
            option_strike=None,
            option_expiry=None,
            option_right=None,
        )
        for position in snapshot.portfolio.positions
    )
    return _build_reference(
        book_ref=snapshot.snapshot_id,
        valuation_ts=snapshot.valuation_ts,
        base_currency=snapshot.base_currency,
        positions=positions,
        content_sha256=None,
    )


def _read_regular_legacy_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        if not getattr(os, "O_NOFOLLOW", 0) and path.is_symlink():
            raise NonRegularLegacyBookFileError("legacy book path is a symlink")
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise LegacyBookNotFoundError(f"legacy book is missing: {path.name}") from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EISDIR}:
            raise NonRegularLegacyBookFileError(
                f"legacy book path is not a regular file: {path.name}"
            ) from error
        raise LegacyBookCorruptError(f"legacy book cannot be opened: {path.name}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NonRegularLegacyBookFileError(
                f"legacy book path is not a regular file: {path.name}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise LegacyBookCorruptError(f"legacy book cannot be read: {path.name}") from error
    finally:
        os.close(descriptor)


def adapt_legacy_book_payload(
    payload: bytes,
    *,
    expected_book_ref: str | None = None,
) -> LegacyBookReferenceV1:
    if expected_book_ref is not None:
        _validate_book_ref(expected_book_ref)
    try:
        parsed = load_unambiguous_json_bytes(payload)
    except ManifestError as error:
        raise LegacyBookCorruptError(f"legacy book JSON is ambiguous: {error}") from error
    if not isinstance(parsed, dict):
        raise LegacyBookCorruptError("legacy book JSON must be an object")
    try:
        legacy = LegacyBookPayloadV0.model_validate_json(payload)
    except ValueError as error:
        raise LegacyBookCorruptError("legacy book payload violates its frozen shape") from error
    if expected_book_ref is not None and legacy.snapshot_id != expected_book_ref:
        raise LegacyBookCorruptError(
            "legacy book embedded snapshot ID does not match the requested book ref"
        )
    positions = tuple(
        LegacyPositionSummaryV1(
            con_id=position.con_id,
            symbol=position.symbol,
            quantity=position.qty,
            multiplier=position.multiplier,
            security_type=position.sec_type,
            option_strike=position.strike,
            option_expiry=position.expiry,
            option_right=position.right,
        )
        for position in legacy.positions
    )
    return _build_reference(
        book_ref=legacy.snapshot_id,
        valuation_ts=legacy.valuation_ts,
        base_currency=legacy.base_currency,
        positions=positions,
        content_sha256=hashlib.sha256(payload).hexdigest(),
    )


def read_legacy_book(root: Path, book_ref: str) -> LegacyBookReferenceV1:
    book_ref = _validate_book_ref(book_ref)
    path = Path(root) / "books" / f"{book_ref}.json"
    payload = _read_regular_legacy_bytes(path)
    return adapt_legacy_book_payload(payload, expected_book_ref=book_ref)


def legacy_source_reference(root: Path, book_ref: str) -> LegacyBookReferenceV1:
    return read_legacy_book(root, book_ref)


__all__ = [
    "InvalidLegacyBookRefError",
    "LegacyBookCorruptError",
    "LegacyBookError",
    "LegacyBookNotFoundError",
    "LegacyBookPayloadV0",
    "LegacyBookPositionV0",
    "LegacyBookReferenceV1",
    "LegacyPositionSummaryV1",
    "NonRegularLegacyBookFileError",
    "adapt_legacy_book_payload",
    "adapt_legacy_book_snapshot",
    "legacy_source_reference",
    "read_legacy_book",
]
