"""Durable SQLite ownership for snapshot runs, publications, and active pointers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Literal

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import (
    FrozenContractBase,
    RunOutcome,
    RunStage,
    SnapshotStatus,
    canonical_json_bytes,
)
from quantmind.snapshots.store import (
    ArtifactDigestMismatchError,
    ArtifactLengthMismatchError,
    ArtifactNotFoundError,
    ManifestFilenameMismatchError,
    NonRegularSnapshotFileError,
    SnapshotVerificationError,
    SnapshotVerifier,
    VerifiedSnapshotV1,
    select_last_good,
)
from quantmind.snapshots.manifest import (
    ManifestError,
    ManifestIdentityError,
    verify_manifest,
)


_CURRENT_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5_000
_MAX_RESULT_BYTES = 1_024
_MAX_RECOVERY_JSON_BYTES = 65_536
_MAX_RECOVERY_FAILURES = 128
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
_CANONICAL_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_STAGE_ORDER = (
    RunStage.QUEUED,
    RunStage.INGESTING,
    RunStage.RECONCILING,
    RunStage.VALIDATING,
    RunStage.MODELING,
    RunStage.PUBLISHING,
)
_EXPECTED_SCHEMA_COLUMNS = {
    "book_heads": {
        "book_id", "generation", "canonical_book_ref", "updated_at_utc", "version"
    },
    "snapshot_runs": {
        "run_id", "run_kind", "idempotency_identity", "request_fingerprint",
        "client_idempotency_key_digest", "book_id", "captured_generation",
        "expected_active_snapshot_id", "expected_active_pointer_version",
        "target_cut_utc", "requested_at_utc", "started_at_utc", "updated_at_utc",
        "finished_at_utc", "run_stage", "run_outcome", "cancel_requested_at_utc",
        "candidate_snapshot_id", "published_snapshot_id", "result_json", "error_code",
        "error_message", "version",
    },
    "snapshot_manifests": {
        "publication_sequence", "snapshot_id", "run_id", "book_id", "book_generation",
        "snapshot_status", "schema_version", "hash_algorithm", "manifest_relpath",
        "envelope_sha256", "envelope_byte_length", "published_at_utc",
    },
    "active_snapshots": {
        "book_id", "snapshot_id", "book_generation", "pointer_version", "updated_at_utc"
    },
    "snapshot_recovery_events": {
        "event_sequence", "book_id", "rejected_snapshot_id", "expected_pointer_version",
        "resolution_action", "selected_snapshot_id", "detail_json", "recorded_at_utc",
    },
}
_EXPECTED_SCHEMA_INDEXES = {
    "one_live_idempotency_identity",
    "one_live_snapshot_per_book_generation",
    "snapshot_runs_by_identity",
    "snapshot_runs_by_preimage",
    "snapshot_runs_by_book_generation",
    "snapshot_runs_by_book_pointer_version",
    "snapshot_runs_by_book_requested",
    "blessed_manifest_fallback",
    "snapshot_manifests_by_book_generation",
    "snapshot_manifests_by_book_sequence",
    "recovery_events_by_book_sequence",
    "recovery_events_by_book_pointer_version",
}
_EXPECTED_INDEX_SHAPES = {
    "one_live_idempotency_identity": (
        "snapshot_runs", ("run_kind", "idempotency_identity"), 1, 1
    ),
    "one_live_snapshot_per_book_generation": (
        "snapshot_runs", ("book_id", "captured_generation"), 1, 1
    ),
    "snapshot_runs_by_identity": (
        "snapshot_runs", ("run_kind", "idempotency_identity"), 0, 0
    ),
    "snapshot_runs_by_preimage": (
        "snapshot_runs",
        (
            "run_kind",
            "request_fingerprint",
            "client_idempotency_key_digest",
            "book_id",
            "captured_generation",
            "target_cut_utc",
        ),
        0,
        0,
    ),
    "snapshot_runs_by_book_generation": (
        "snapshot_runs", ("book_id", "captured_generation"), 0, 0
    ),
    "snapshot_runs_by_book_pointer_version": (
        "snapshot_runs", ("book_id", "expected_active_pointer_version"), 0, 1
    ),
    "snapshot_runs_by_book_requested": (
        "snapshot_runs", ("book_id", "requested_at_utc", "run_id"), 0, 0
    ),
    "blessed_manifest_fallback": (
        "snapshot_manifests", ("book_id", "publication_sequence"), 0, 1
    ),
    "snapshot_manifests_by_book_generation": (
        "snapshot_manifests", ("book_id", "book_generation"), 0, 0
    ),
    "snapshot_manifests_by_book_sequence": (
        "snapshot_manifests", ("book_id", "publication_sequence"), 0, 0
    ),
    "recovery_events_by_book_sequence": (
        "snapshot_recovery_events", ("book_id", "event_sequence"), 0, 0
    ),
    "recovery_events_by_book_pointer_version": (
        "snapshot_recovery_events",
        ("book_id", "expected_pointer_version", "event_sequence"),
        0,
        1,
    ),
}
_EXPECTED_FOREIGN_KEY_GROUPS = tuple(
    sorted(
        (
            table,
            parent,
            columns,
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
        for table, parent, columns in (
            (
                "active_snapshots",
                "book_heads",
                (("book_id", "book_id"),),
            ),
            (
                "active_snapshots",
                "snapshot_manifests",
                (
                    ("book_id", "book_id"),
                    ("snapshot_id", "snapshot_id"),
                    ("book_generation", "book_generation"),
                ),
            ),
            (
                "snapshot_manifests",
                "book_heads",
                (("book_id", "book_id"),),
            ),
            (
                "snapshot_manifests",
                "snapshot_runs",
                (("run_id", "run_id"),),
            ),
            (
                "snapshot_recovery_events",
                "book_heads",
                (("book_id", "book_id"),),
            ),
            (
                "snapshot_recovery_events",
                "snapshot_manifests",
                (
                    ("book_id", "book_id"),
                    ("rejected_snapshot_id", "snapshot_id"),
                ),
            ),
            (
                "snapshot_recovery_events",
                "snapshot_manifests",
                (
                    ("book_id", "book_id"),
                    ("selected_snapshot_id", "snapshot_id"),
                ),
            ),
            (
                "snapshot_runs",
                "book_heads",
                (("book_id", "book_id"),),
            ),
            (
                "snapshot_runs",
                "snapshot_manifests",
                (
                    ("book_id", "book_id"),
                    ("expected_active_snapshot_id", "snapshot_id"),
                ),
            ),
        )
    )
)


class RunRepositoryError(RuntimeError):
    """Base class for typed durable-run failures."""


class RunNotFoundError(RunRepositoryError):
    pass


class IllegalRunTransitionError(RunRepositoryError):
    pass


class StaleRunVersionError(RunRepositoryError):
    pass


class TerminalRunMutationError(RunRepositoryError):
    pass


class GenerationRegressionError(RunRepositoryError):
    pass


class IncompatibleLiveRunError(RunRepositoryError):
    pass


class PublicationConflictError(RunRepositoryError):
    pass


class RunDatabaseError(RunRepositoryError):
    pass


class RunErrorCode(str, Enum):
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    WORKER_FAILED = "WORKER_FAILED"
    SERIALIZATION_FAILED = "SERIALIZATION_FAILED"
    BROKEN_PROCESS_POOL = "BROKEN_PROCESS_POOL"
    DISK_WRITE_FAILED = "DISK_WRITE_FAILED"
    DATABASE_FAILED = "DATABASE_FAILED"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    INTERRUPTED = "INTERRUPTED"
    STALE_BOOK_GENERATION = "STALE_BOOK_GENERATION"
    STALE_ACTIVE_POINTER = "STALE_ACTIVE_POINTER"
    HARD_GATE_FAILED = "HARD_GATE_FAILED"
    SHUTDOWN_INTERRUPTED = "SHUTDOWN_INTERRUPTED"


_PUBLICATION_REJECTION_CODES = frozenset(
    {
        RunErrorCode.CANCELLED_BY_USER,
        RunErrorCode.STALE_BOOK_GENERATION,
        RunErrorCode.STALE_ACTIVE_POINTER,
    }
)


_ERROR_MESSAGES: dict[RunErrorCode, str] = {
    RunErrorCode.SUBMISSION_FAILED: "executor submission failed",
    RunErrorCode.WORKER_FAILED: "worker execution failed",
    RunErrorCode.SERIALIZATION_FAILED: "result serialization failed",
    RunErrorCode.BROKEN_PROCESS_POOL: "worker pool unavailable",
    RunErrorCode.DISK_WRITE_FAILED: "durable artifact write failed",
    RunErrorCode.DATABASE_FAILED: "durable catalog operation failed",
    RunErrorCode.CANCELLED_BY_USER: "cancelled by user",
    RunErrorCode.INTERRUPTED: "run interrupted by process restart",
    RunErrorCode.STALE_BOOK_GENERATION: (
        "canonical book generation changed before publication"
    ),
    RunErrorCode.STALE_ACTIVE_POINTER: (
        "active snapshot pointer changed before publication"
    ),
    RunErrorCode.HARD_GATE_FAILED: "analytical hard gate failed",
    RunErrorCode.SHUTDOWN_INTERRUPTED: "run interrupted by shutdown",
}


class RunResultCode(str, Enum):
    EMPTY = "EMPTY"
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    SYNC_COMPLETED = "SYNC_COMPLETED"
    ARTIFACT_REFERENCE = "ARTIFACT_REFERENCE"


class RecoveryRejectionCode(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    INVALID_MANIFEST = "INVALID_MANIFEST"
    INVALID_VERIFIER_RESULT = "INVALID_VERIFIER_RESULT"
    NOT_BLESSED = "NOT_BLESSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class _RecoveryNotFound(Exception):
    pass


class _RecoveryIdentityMismatch(Exception):
    pass


class _RecoveryIntegrityFailure(Exception):
    pass


class _RecoveryInvalidManifest(Exception):
    pass


class _RecoveryInvalidVerifierResult(Exception):
    pass


class _RecoveryVerificationFailed(Exception):
    pass


_SELECTOR_REJECTION_CODES = {
    _RecoveryNotFound.__name__: RecoveryRejectionCode.NOT_FOUND,
    _RecoveryIdentityMismatch.__name__: RecoveryRejectionCode.IDENTITY_MISMATCH,
    _RecoveryIntegrityFailure.__name__: RecoveryRejectionCode.INTEGRITY_FAILURE,
    _RecoveryInvalidManifest.__name__: RecoveryRejectionCode.INVALID_MANIFEST,
    _RecoveryInvalidVerifierResult.__name__: RecoveryRejectionCode.INVALID_VERIFIER_RESULT,
    _RecoveryVerificationFailed.__name__: RecoveryRejectionCode.VERIFICATION_FAILED,
    "NOT_BLESSED": RecoveryRejectionCode.NOT_BLESSED,
}


class ActiveRecoveryDecision(str, Enum):
    UNCHANGED = "UNCHANGED"
    REPOINTED = "REPOINTED"
    REMOVED = "REMOVED"
    CAS_LOST = "CAS_LOST"


def _require_nonblank(value: str, field_name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    value = unicodedata.normalize("NFC", value)
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} exceeds its bounded length")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _normalized_transient_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("client idempotency key must be a string")
    value = unicodedata.normalize("NFC", value)
    if not value.strip():
        raise ValueError("client idempotency key must be nonblank")
    if len(value.encode("utf-8")) > 4_096:
        raise ValueError("client idempotency key exceeds its transient bound")
    return value


def _require_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def _require_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be explicitly UTC")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime, field_name: str = "timestamp") -> str:
    return _require_utc(value, field_name).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CANONICAL_TIMESTAMP_RE.fullmatch(value):
        raise ValueError("durable timestamp is not canonical fixed-width UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("durable timestamp is not explicitly UTC")
    return parsed.astimezone(UTC)


def _require_expected_version(value: int | None, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected version must be an integer")
    if value < 1:
        raise ValueError("expected version must be positive")
    return value


def _require_monotonic_update(row: sqlite3.Row, now_text: str) -> None:
    if now_text < row["requested_at_utc"] or now_text < row["updated_at_utc"]:
        raise ValueError("run lifecycle time cannot move backward")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _validate_canonical_json_text(value: str, *, maximum_bytes: int) -> Any:
    if not isinstance(value, str):
        raise TypeError("durable JSON must be a string")
    payload = value.encode("utf-8")
    if len(payload) > maximum_bytes:
        raise ValueError("durable JSON exceeds its size limit")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("durable JSON is invalid") from error
    if canonical_json_bytes(parsed) != payload:
        raise ValueError("durable JSON must use canonical JSON encoding")
    return parsed


def _idempotency_identity_from_preimage(
    *,
    run_kind: str,
    request_fingerprint: str,
    client_idempotency_key_digest: str | None,
    book_id: str | None,
    captured_generation: int | None,
    target_cut_utc: datetime | None,
) -> str:
    payload = {
        "book_id": book_id,
        "captured_generation": captured_generation,
        "client_idempotency_key_digest": client_idempotency_key_digest,
        "request_fingerprint": request_fingerprint,
        "run_kind": run_kind,
        "target_cut_utc": target_cut_utc,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class NewRunV1(FrozenContractBase):
    run_id: str
    run_kind: str
    request_fingerprint: str
    client_idempotency_key: str | None
    book_id: str | None
    target_cut_utc: datetime | None

    @field_validator("run_id")
    @classmethod
    def _run_id_is_full_opaque(cls, value: str) -> str:
        if not _OPAQUE_ID_RE.fullmatch(value):
            raise ValueError("run ID must be a full bounded opaque identifier")
        return value

    @field_validator("run_kind")
    @classmethod
    def _run_kind_is_explicit(cls, value: str) -> str:
        return _require_nonblank(value, "run kind", limit=64)

    @field_validator("request_fingerprint")
    @classmethod
    def _fingerprint_is_full(cls, value: str) -> str:
        return _require_digest(value, "request fingerprint")

    @field_validator("client_idempotency_key", "book_id")
    @classmethod
    def _optional_identifiers_are_bounded(
        cls, value: str | None, info
    ) -> str | None:
        if value is None:
            return None
        if info.field_name == "client_idempotency_key":
            return _normalized_transient_key(value)
        return _require_nonblank(value, info.field_name)

    @field_validator("target_cut_utc")
    @classmethod
    def _target_cut_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "target cut")

    @model_validator(mode="after")
    def _book_fields_are_coherent(self) -> "NewRunV1":
        if (self.book_id is None) != (self.target_cut_utc is None):
            raise ValueError("book ID and target cut must both be supplied or both be null")
        return self


class RunFailureV1(FrozenContractBase):
    code: RunErrorCode

    @field_validator("code")
    @classmethod
    def _generic_failure_cannot_claim_reserved_catalog_evidence(
        cls, value: RunErrorCode
    ) -> RunErrorCode:
        if value in _PUBLICATION_REJECTION_CODES:
            if value is RunErrorCode.CANCELLED_BY_USER:
                raise ValueError(
                    "CANCELLED_BY_USER requires durable cancellation acknowledgement"
                )
            raise ValueError(
                f"{value.value} is reserved for atomic publication rejection"
            )
        return value


class RunResultV1(FrozenContractBase):
    schema_version: Literal["durable_run_result_v1"]
    result_code: RunResultCode
    boolean_value: bool | None
    integer_value: int | None
    artifact_digest: str | None

    @field_validator("integer_value")
    @classmethod
    def _integer_is_exact(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise TypeError("integer result must be an exact integer")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_is_full(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "result artifact digest")

    @model_validator(mode="after")
    def _payload_matches_closed_result_code(self) -> "RunResultV1":
        populated = (
            self.boolean_value is not None,
            self.integer_value is not None,
            self.artifact_digest is not None,
        )
        expected = {
            RunResultCode.EMPTY: (False, False, False),
            RunResultCode.BOOLEAN: (True, False, False),
            RunResultCode.INTEGER: (False, True, False),
            RunResultCode.SYNC_COMPLETED: (False, True, False),
            RunResultCode.ARTIFACT_REFERENCE: (False, False, True),
        }[self.result_code]
        if populated != expected:
            raise ValueError("result payload does not match its closed result code")
        if (
            self.result_code is RunResultCode.SYNC_COMPLETED
            and self.integer_value is not None
            and self.integer_value < 0
        ):
            raise ValueError("sync symbol count must be nonnegative")
        return self


def adapt_legacy_result(value: object) -> RunResultV1:
    """Explicit T3C seam from the legacy executor's narrow safe result vocabulary."""

    fields: dict[str, object] = {
        "schema_version": "durable_run_result_v1",
        "boolean_value": None,
        "integer_value": None,
        "artifact_digest": None,
    }
    if value is None:
        fields["result_code"] = RunResultCode.EMPTY
    elif isinstance(value, bool):
        fields.update(result_code=RunResultCode.BOOLEAN, boolean_value=value)
    elif isinstance(value, int):
        fields.update(result_code=RunResultCode.INTEGER, integer_value=value)
    elif isinstance(value, str):
        match = re.fullmatch(r"synced ([0-9]+) symbols", value)
        if match is None:
            raise ValueError("legacy text result is outside the allowlisted adapter vocabulary")
        fields.update(
            result_code=RunResultCode.SYNC_COMPLETED,
            integer_value=int(match.group(1)),
        )
    else:
        raise TypeError("legacy result type is outside the allowlisted adapter vocabulary")
    return RunResultV1(**fields)


class BookHeadV1(FrozenContractBase):
    book_id: str
    generation: int = Field(ge=0)
    canonical_book_ref: str
    updated_at_utc: datetime
    version: int = Field(ge=1)

    @field_validator("book_id")
    @classmethod
    def _book_id_is_normalized(cls, value: str) -> str:
        normalized = _require_nonblank(value, "book ID")
        if normalized != value:
            raise ValueError("stored book ID must already be NFC normalized")
        return value

    @field_validator("canonical_book_ref")
    @classmethod
    def _book_ref_is_full(cls, value: str) -> str:
        return _require_digest(value, "canonical book reference")


class RunRecordV1(FrozenContractBase):
    run_id: str
    run_kind: str
    idempotency_identity: str
    request_fingerprint: str
    client_idempotency_key_digest: str | None
    book_id: str | None
    captured_generation: int | None = Field(ge=0)
    expected_active_snapshot_id: str | None
    expected_active_pointer_version: int = Field(ge=0)
    target_cut_utc: datetime | None
    requested_at_utc: datetime
    started_at_utc: datetime | None
    updated_at_utc: datetime
    finished_at_utc: datetime | None
    run_stage: RunStage
    run_outcome: RunOutcome
    cancel_requested_at_utc: datetime | None
    candidate_snapshot_id: str | None
    published_snapshot_id: str | None
    result: RunResultV1 | None
    error_code: RunErrorCode | None
    error_message: str | None
    version: int = Field(ge=1)

    @field_validator("run_id")
    @classmethod
    def _run_id_is_valid(cls, value: str) -> str:
        if not _OPAQUE_ID_RE.fullmatch(value):
            raise ValueError("run ID must be a full bounded opaque identifier")
        return value

    @field_validator("run_kind", "book_id")
    @classmethod
    def _stored_identity_is_normalized(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        normalized = _require_nonblank(
            value,
            info.field_name,
            limit=64 if info.field_name == "run_kind" else 256,
        )
        if normalized != value:
            raise ValueError(f"stored {info.field_name} must already be NFC normalized")
        return value

    @field_validator(
        "idempotency_identity",
        "request_fingerprint",
        "client_idempotency_key_digest",
        "expected_active_snapshot_id",
        "candidate_snapshot_id",
        "published_snapshot_id",
    )
    @classmethod
    def _stored_digests_are_full(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def _lifecycle_is_coherent(self) -> "RunRecordV1":
        book_fields_present = (
            self.book_id is not None,
            self.captured_generation is not None,
            self.target_cut_utc is not None,
        )
        if any(book_fields_present) and not all(book_fields_present):
            raise ValueError("durable book identity tuple is incomplete")
        if (
            self.expected_active_snapshot_id is not None
            and self.expected_active_pointer_version < 1
        ):
            raise ValueError("expected active snapshot requires a positive pointer version")
        if self.book_id is None and (
            self.expected_active_snapshot_id is not None
            or self.expected_active_pointer_version != 0
        ):
            raise ValueError("non-book runs cannot carry active-pointer state")
        if self.updated_at_utc < self.requested_at_utc:
            raise ValueError("run update cannot precede its request")
        for value in (
            self.started_at_utc,
            self.cancel_requested_at_utc,
            self.finished_at_utc,
        ):
            if value is not None and not (
                self.requested_at_utc <= value <= self.updated_at_utc
            ):
                raise ValueError("run lifecycle timestamps are not monotonic")
        if self.finished_at_utc is not None:
            if (
                self.started_at_utc is not None
                and self.finished_at_utc < self.started_at_utc
            ):
                raise ValueError("run finish cannot precede its start")
            if (
                self.cancel_requested_at_utc is not None
                and self.finished_at_utc < self.cancel_requested_at_utc
            ):
                raise ValueError("run finish cannot precede cancellation intent")
        if (self.run_outcome is RunOutcome.RUNNING) != (
            self.finished_at_utc is None
        ):
            raise ValueError("running and terminal finish state is incoherent")
        if self.error_code is None:
            if self.error_message is not None:
                raise ValueError("error message requires a typed error code")
        elif self.error_message != _ERROR_MESSAGES[self.error_code]:
            raise ValueError("durable error message is not the curated code message")
        if self.run_outcome in {RunOutcome.RUNNING, RunOutcome.SUCCEEDED}:
            if self.error_code is not None:
                raise ValueError("running and successful runs cannot carry failure evidence")
        elif self.run_outcome is RunOutcome.FAILED:
            if (
                self.error_code is None
                or self.error_code is RunErrorCode.CANCELLED_BY_USER
            ):
                raise ValueError("failed runs require non-cancellation failure evidence")
        else:
            if (
                self.error_code is not RunErrorCode.CANCELLED_BY_USER
                or self.cancel_requested_at_utc is None
            ):
                raise ValueError("cancelled outcome requires durable cancellation intent")
        if self.result is not None and (
            self.run_outcome is not RunOutcome.SUCCEEDED or self.book_id is not None
        ):
            raise ValueError(
                "durable results are valid only for successful non-book runs"
            )
        if (
            self.published_snapshot_id is not None
            and self.run_outcome is not RunOutcome.SUCCEEDED
        ):
            raise ValueError("published snapshot identity requires successful outcome")
        if self.book_id is None and (
            self.candidate_snapshot_id is not None
            or self.published_snapshot_id is not None
        ):
            raise ValueError("non-book runs cannot carry snapshot identity")
        if self.run_outcome is RunOutcome.SUCCEEDED and self.book_id is not None:
            if (
                self.run_stage is not RunStage.PUBLISHING
                or self.candidate_snapshot_id is None
                or self.published_snapshot_id != self.candidate_snapshot_id
            ):
                raise ValueError(
                    "successful book runs require their exact candidate publication"
                )
        return self


class CreateRunResultV1(FrozenContractBase):
    record: RunRecordV1
    created: bool


class ConnectionPragmasV1(FrozenContractBase):
    foreign_keys: int
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int


class ManifestPublicationV1(FrozenContractBase):
    snapshot_id: str
    book_id: str
    book_generation: int = Field(ge=0)
    snapshot_status: SnapshotStatus
    schema_version: Literal["analytical_snapshot_manifest_v1"]
    hash_algorithm: Literal["sha256"]
    manifest_relpath: str
    envelope_sha256: str
    envelope_byte_length: int = Field(ge=0)

    @field_validator("snapshot_id", "envelope_sha256")
    @classmethod
    def _digests_are_full(cls, value: str, info) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("book_id")
    @classmethod
    def _book_id_is_bounded(cls, value: str) -> str:
        return _require_nonblank(value, "book ID")

    @field_validator("manifest_relpath")
    @classmethod
    def _manifest_path_is_store_relative(cls, value: str) -> str:
        _require_nonblank(value, "manifest relative path", limit=512)
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("manifest path must be a normalized store-relative path")
        return value

    @model_validator(mode="after")
    def _path_binds_snapshot_identity(self) -> "ManifestPublicationV1":
        expected = (
            "snapshots/manifests/analytical_snapshot_manifest_v1/"
            f"{self.snapshot_id[:2]}/{self.snapshot_id}.json"
        )
        if self.manifest_relpath != expected:
            raise ValueError("manifest path does not bind the full snapshot identity")
        return self


class ManifestPublicationRecordV1(FrozenContractBase):
    publication_sequence: int = Field(ge=1)
    snapshot_id: str
    run_id: str
    book_id: str
    book_generation: int = Field(ge=0)
    snapshot_status: SnapshotStatus
    schema_version: str
    hash_algorithm: Literal["sha256"]
    manifest_relpath: str
    envelope_sha256: str
    envelope_byte_length: int = Field(ge=0)
    published_at_utc: datetime

    @model_validator(mode="after")
    def _metadata_is_strict(self) -> "ManifestPublicationRecordV1":
        ManifestPublicationV1(
            snapshot_id=self.snapshot_id,
            book_id=self.book_id,
            book_generation=self.book_generation,
            snapshot_status=self.snapshot_status,
            schema_version=self.schema_version,
            hash_algorithm=self.hash_algorithm,
            manifest_relpath=self.manifest_relpath,
            envelope_sha256=self.envelope_sha256,
            envelope_byte_length=self.envelope_byte_length,
        )
        if not _OPAQUE_ID_RE.fullmatch(self.run_id):
            raise ValueError("publication run ID is malformed")
        return self


class ActiveSnapshotV1(FrozenContractBase):
    book_id: str
    snapshot_id: str
    book_generation: int = Field(ge=0)
    pointer_version: int = Field(ge=1)
    updated_at_utc: datetime

    @field_validator("book_id")
    @classmethod
    def _book_id_is_normalized(cls, value: str) -> str:
        normalized = _require_nonblank(value, "book ID")
        if normalized != value:
            raise ValueError("stored book ID must already be NFC normalized")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_is_full(cls, value: str) -> str:
        return _require_digest(value, "active snapshot ID")


class _ActivePointerRegisterV1(FrozenContractBase):
    book_id: str
    snapshot_id: str | None
    book_generation: int | None = Field(ge=0)
    pointer_version: int = Field(ge=0)
    updated_at_utc: datetime

    @field_validator("book_id")
    @classmethod
    def _book_id_is_normalized(cls, value: str) -> str:
        normalized = _require_nonblank(value, "book ID")
        if normalized != value:
            raise ValueError("stored book ID must already be NFC normalized")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_is_full(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "active snapshot ID")

    @model_validator(mode="after")
    def _state_is_coherent(self) -> "_ActivePointerRegisterV1":
        if (self.snapshot_id is None) != (self.book_generation is None):
            raise ValueError("pointer identity and generation must be present together")
        if self.snapshot_id is not None and self.pointer_version < 1:
            raise ValueError("live active pointer requires a positive version")
        return self

    def active_snapshot(self) -> ActiveSnapshotV1 | None:
        if self.snapshot_id is None or self.book_generation is None:
            return None
        return ActiveSnapshotV1(
            book_id=self.book_id,
            snapshot_id=self.snapshot_id,
            book_generation=self.book_generation,
            pointer_version=self.pointer_version,
            updated_at_utc=self.updated_at_utc,
        )


class _PointerTransitionV1(FrozenContractBase):
    book_id: str
    previous_snapshot_id: str | None
    selected_snapshot_id: str | None
    pointer_version: int = Field(ge=1)
    transitioned_at_utc: datetime
    transition_kind: Literal["PUBLICATION", "REPOINTED", "REMOVED"]

    @field_validator("book_id")
    @classmethod
    def _book_id_is_normalized(cls, value: str) -> str:
        normalized = _require_nonblank(value, "book ID")
        if normalized != value:
            raise ValueError("stored book ID must already be NFC normalized")
        return value

    @field_validator("previous_snapshot_id", "selected_snapshot_id")
    @classmethod
    def _snapshot_ids_are_full(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def _transition_is_coherent(self) -> "_PointerTransitionV1":
        if self.transition_kind == "REMOVED":
            if self.previous_snapshot_id is None or self.selected_snapshot_id is not None:
                raise ValueError("removal transition is malformed")
        elif self.selected_snapshot_id is None:
            raise ValueError("live pointer transition requires a selected snapshot")
        if self.previous_snapshot_id == self.selected_snapshot_id:
            raise ValueError("pointer transition must change state")
        return self


class PublicationResultV1(FrozenContractBase):
    run: RunRecordV1
    publication: ManifestPublicationRecordV1 | None
    active: ActiveSnapshotV1 | None
    published: bool
    already_published: bool
    rejection_code: RunErrorCode | None

    @model_validator(mode="after")
    def _records_describe_one_publication_attempt(self) -> "PublicationResultV1":
        # Nested model instances may have been created through model_copy or
        # model_construct. Reparse their primitive fields before trusting any
        # cross-record relationship in this security boundary contract.
        RunRecordV1.model_validate(
            self.run.model_dump(mode="python", warnings=False)
        )
        if self.publication is not None:
            ManifestPublicationRecordV1.model_validate(
                self.publication.model_dump(mode="python", warnings=False)
            )
        if self.active is not None:
            ActiveSnapshotV1.model_validate(
                self.active.model_dump(mode="python", warnings=False)
            )

        publication = self.publication
        run = self.run
        if self.published != (publication is not None):
            raise ValueError("published flag must match durable publication presence")
        if self.already_published and publication is None:
            raise ValueError("already-published result requires a publication")
        if run.book_id is None or run.captured_generation is None:
            raise ValueError("publication results require a durable book identity")
        if self.active is not None and self.active.book_id != run.book_id:
            raise ValueError("active snapshot belongs to a different book")

        if publication is not None:
            if run.run_outcome is not RunOutcome.SUCCEEDED:
                raise ValueError("publication presence requires a successful run")
            if self.rejection_code is not None:
                raise ValueError("successful publication cannot carry rejection evidence")
            if (
                publication.run_id != run.run_id
                or publication.book_id != run.book_id
                or publication.book_generation != run.captured_generation
                or publication.snapshot_id != run.candidate_snapshot_id
                or publication.snapshot_id != run.published_snapshot_id
            ):
                raise ValueError("publication identity does not bind the durable run")
            if (
                run.finished_at_utc is None
                or publication.published_at_utc != run.finished_at_utc
                or publication.published_at_utc != run.updated_at_utc
            ):
                raise ValueError("publication and successful run clocks are not bound")
            publication_pointer_version = run.expected_active_pointer_version + 1
            if self.active is not None:
                if self.active.pointer_version < publication_pointer_version:
                    raise ValueError("active pointer predates the publication")
                if self.active.pointer_version == publication_pointer_version and (
                    self.active.snapshot_id != publication.snapshot_id
                    or self.active.book_generation != publication.book_generation
                    or self.active.updated_at_utc != publication.published_at_utc
                ):
                    raise ValueError("publication pointer state is not exact")
                if self.active.updated_at_utc < publication.published_at_utc:
                    raise ValueError("active pointer clock predates the publication")
            return self

        if self.already_published:
            raise ValueError("rejected publication cannot already be published")
        if self.rejection_code not in _PUBLICATION_REJECTION_CODES:
            raise ValueError("publication result uses an open rejection shape")
        if run.run_stage is not RunStage.PUBLISHING:
            raise ValueError("publication rejection requires the PUBLISHING stage")
        if run.error_code is not self.rejection_code:
            raise ValueError("publication rejection does not bind durable run evidence")
        if self.rejection_code is RunErrorCode.CANCELLED_BY_USER:
            if run.run_outcome is not RunOutcome.CANCELLED:
                raise ValueError("cancellation rejection requires cancelled outcome")
        elif run.run_outcome is not RunOutcome.FAILED:
            raise ValueError("stale publication rejection requires failed outcome")
        if run.published_snapshot_id is not None:
            raise ValueError("rejected publication cannot bind a published snapshot")
        if (
            self.rejection_code
            in {
                RunErrorCode.STALE_BOOK_GENERATION,
                RunErrorCode.STALE_ACTIVE_POINTER,
            }
            and run.candidate_snapshot_id is None
        ):
            raise ValueError("stale publication rejection requires an attached candidate")

        if self.active is not None:
            expected_version = run.expected_active_pointer_version
            if self.active.pointer_version < expected_version:
                raise ValueError("active pointer predates the captured predecessor")
            if (
                self.active.pointer_version > expected_version
                and self.active.updated_at_utc < run.requested_at_utc
            ):
                raise ValueError("later active pointer clock predates the run request")
            if self.active.pointer_version == expected_version:
                if (
                    run.expected_active_snapshot_id is None
                    or self.active.snapshot_id != run.expected_active_snapshot_id
                ):
                    raise ValueError("active pointer does not match the captured predecessor")
                if self.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER:
                    raise ValueError("stale-pointer rejection requires a later pointer")
        return self


class RecoveryEventV1(FrozenContractBase):
    event_sequence: int = Field(ge=1)
    book_id: str
    rejected_snapshot_id: str
    expected_pointer_version: int = Field(ge=1)
    resolution_action: ActiveRecoveryDecision
    selected_snapshot_id: str | None
    detail_json: str
    recorded_at_utc: datetime

    @field_validator("book_id")
    @classmethod
    def _book_id_is_normalized(cls, value: str) -> str:
        normalized = _require_nonblank(value, "book ID")
        if normalized != value:
            raise ValueError("stored book ID must already be NFC normalized")
        return value

    @field_validator("rejected_snapshot_id", "selected_snapshot_id")
    @classmethod
    def _snapshot_ids_are_full(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_digest(value, info.field_name)

    @field_validator("detail_json")
    @classmethod
    def _detail_is_closed_and_bounded(cls, value: str) -> str:
        parsed = _validate_canonical_json_text(
            value, maximum_bytes=_MAX_RECOVERY_JSON_BYTES
        )
        if not isinstance(parsed, dict) or set(parsed) != {"failures", "omitted_count"}:
            raise ValueError("recovery detail fields are not the closed v1 shape")
        if not isinstance(parsed["omitted_count"], int) or isinstance(
            parsed["omitted_count"], bool
        ) or parsed["omitted_count"] < 0:
            raise ValueError("recovery omitted count is invalid")
        failures = parsed["failures"]
        if not isinstance(failures, list) or len(failures) > _MAX_RECOVERY_FAILURES:
            raise ValueError("recovery failures exceed the deterministic cap")
        for failure in failures:
            if not isinstance(failure, dict) or set(failure) != {
                "error_code",
                "snapshot_id",
            }:
                raise ValueError("recovery failure fields are invalid")
            RecoveryRejectionCode(failure["error_code"])
            _require_digest(failure["snapshot_id"], "recovery snapshot ID")
        return value

    @model_validator(mode="after")
    def _action_and_selection_are_coherent(self) -> "RecoveryEventV1":
        if self.resolution_action is ActiveRecoveryDecision.UNCHANGED:
            raise ValueError("UNCHANGED recovery does not create durable evidence")
        if self.resolution_action is ActiveRecoveryDecision.REPOINTED:
            if self.selected_snapshot_id is None:
                raise ValueError("REPOINTED recovery requires a selected snapshot")
            if self.selected_snapshot_id == self.rejected_snapshot_id:
                raise ValueError("REPOINTED recovery must change snapshot identity")
        elif (
            self.resolution_action is ActiveRecoveryDecision.REMOVED
            and self.selected_snapshot_id is not None
        ):
            raise ValueError("REMOVED recovery cannot retain a selected snapshot")
        if (
            self.resolution_action is ActiveRecoveryDecision.CAS_LOST
            and self.selected_snapshot_id is not None
            and self.selected_snapshot_id == self.rejected_snapshot_id
        ):
            raise ValueError("CAS_LOST selection must differ from the rejected snapshot")
        return self


class ActiveRecoveryResultV1(FrozenContractBase):
    decision: ActiveRecoveryDecision
    previous_active: ActiveSnapshotV1 | None
    active: ActiveSnapshotV1 | None
    event: RecoveryEventV1 | None


RepositoryFaultInjector = Callable[[str], None]


class RunRepository:
    """Short-connection SQLite repository; workers never receive this object."""

    _WAL_ENABLE_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        root: Path,
        *,
        fault_injector: RepositoryFaultInjector | None = None,
    ) -> None:
        configured_root = Path(root).resolve(strict=False)
        self._configured_root = configured_root
        self._configured_fault_injector = fault_injector
        self.root = configured_root
        self.database_path = configured_root / "snapshots" / "runs.sqlite3"
        self._fault_injector = fault_injector

    @staticmethod
    def _trusted_construction_config(
        repository: RunRepository,
    ) -> tuple[Path, RepositoryFaultInjector | None]:
        """Read base-constructor inputs without subclass dispatch."""

        try:
            state = RunRepository.__dict__["__dict__"].__get__(repository)
            root = Path(state["_configured_root"]).resolve(strict=False)
            database_path = Path(state["database_path"]).resolve(strict=False)
            fault_injector = state["_configured_fault_injector"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "repository is missing trusted construction configuration"
            ) from error
        expected_database_path = root / "snapshots" / "runs.sqlite3"
        if database_path != expected_database_path:
            raise ValueError(
                "repository database path differs from its configured root"
            )
        return root, fault_injector

    def _inject(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _open_connection(
        self,
        *,
        configure_wal: bool,
        timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            if configure_wal:
                mode = str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                if mode != "wal":
                    raise sqlite3.OperationalError("WAL journal mode was not enabled")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise RunDatabaseError("cannot open the durable run database") from error

    def _connect(self) -> sqlite3.Connection:
        return self._open_connection(configure_wal=True)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
        except RunRepositoryError:
            raise
        except sqlite3.Error as error:
            raise RunDatabaseError("durable run database read failed") from error
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except RunRepositoryError:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise RunDatabaseError("durable run database mutation failed") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _sqlite_is_busy_or_locked(error: BaseException) -> bool:
        if not isinstance(error, sqlite3.OperationalError):
            return False
        error_code = getattr(error, "sqlite_errorcode", None)
        if isinstance(error_code, int) and (error_code & 0xFF) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return True
        message = str(error).lower()
        return "busy" in message or "locked" in message

    def _request_wal_mode(self, connection: sqlite3.Connection) -> str:
        return str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        ).lower()

    def _wal_now(self) -> float:
        return time.monotonic()

    def _wal_sleep(self, duration_seconds: float) -> None:
        time.sleep(duration_seconds)

    def _enable_wal_with_deadline(self) -> None:
        deadline = self._wal_now() + self._WAL_ENABLE_TIMEOUT_SECONDS
        backoff_seconds = 0.005
        last_error: BaseException | None = None
        while True:
            remaining_seconds = deadline - self._wal_now()
            if remaining_seconds <= 0:
                raise RunDatabaseError(
                    "cannot enable WAL after catalog migration"
                ) from last_error
            attempt_timeout_ms = max(
                1,
                min(250, int(remaining_seconds * 1_000)),
            )
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open_connection(
                    configure_wal=False,
                    timeout_ms=attempt_timeout_ms,
                )
                mode = self._request_wal_mode(connection)
                if mode == "wal":
                    return
                last_error = sqlite3.OperationalError(
                    f"SQLite returned journal mode {mode!r}"
                )
            except RunDatabaseError as error:
                cause = error.__cause__
                if not self._sqlite_is_busy_or_locked(cause):
                    raise
                last_error = cause
            except sqlite3.Error as error:
                if not self._sqlite_is_busy_or_locked(error):
                    raise RunDatabaseError(
                        "cannot enable WAL after catalog migration"
                    ) from error
                last_error = error
            finally:
                if connection is not None:
                    connection.close()

            remaining_seconds = deadline - self._wal_now()
            if remaining_seconds <= 0:
                raise RunDatabaseError(
                    "cannot enable WAL after catalog migration"
                ) from last_error
            self._wal_sleep(min(backoff_seconds, remaining_seconds))
            backoff_seconds = min(backoff_seconds * 2, 0.2)

    def initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RunDatabaseError("cannot create the durable run database root") from error
        try:
            migration = (
                resources.files("quantmind.snapshots.migrations")
                .joinpath("0001_run_catalog.sql")
                .read_text(encoding="utf-8")
            )
            migration_statements = self._migration_statements(migration)
            expected_signature = self._expected_schema_signature(
                migration_statements
            )
        except (OSError, sqlite3.Error) as error:
            raise RunDatabaseError("cannot initialize the durable run database") from error

        migration_connection = self._open_connection(
            configure_wal=False,
            timeout_ms=30_000,
        )
        try:
            migration_connection.execute("BEGIN IMMEDIATE")
            version = int(
                migration_connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version == 0:
                for statement in migration_statements:
                    migration_connection.execute(statement)
                version = int(
                    migration_connection.execute("PRAGMA user_version").fetchone()[0]
                )
            if version != _CURRENT_SCHEMA_VERSION:
                raise RunDatabaseError(
                    f"unsupported durable run schema version: {version}"
                )
            self._validate_schema(
                migration_connection, expected_signature=expected_signature
            )
            self._validate_relational_invariants(migration_connection)
            migration_connection.commit()
        except RunRepositoryError:
            migration_connection.rollback()
            raise
        except (OSError, sqlite3.Error) as error:
            migration_connection.rollback()
            raise RunDatabaseError("cannot initialize the durable run database") from error
        finally:
            migration_connection.close()

        self._enable_wal_with_deadline()

        validation_connection = self._open_connection(
            configure_wal=True,
            timeout_ms=30_000,
        )
        try:
            validation_connection.execute("BEGIN")
            self._validate_schema(
                validation_connection, expected_signature=expected_signature
            )
            self._validate_relational_invariants(validation_connection)
        except RunRepositoryError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise RunDatabaseError("cannot initialize the durable run database") from error
        finally:
            validation_connection.close()

    @staticmethod
    def _migration_statements(script: str) -> tuple[str, ...]:
        statements: list[str] = []
        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                statement = pending.strip()
                if statement:
                    statements.append(statement)
                pending = ""
        if pending.strip():
            raise RunDatabaseError("packaged migration contains an incomplete statement")
        return tuple(statements)

    @staticmethod
    def _schema_signature(
        connection: sqlite3.Connection,
    ) -> tuple[tuple[str, str, str, str | None], ...]:
        return tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                ORDER BY type, name, tbl_name, sql
                """
            ).fetchall()
        )

    @staticmethod
    def _foreign_key_signature(
        connection: sqlite3.Connection,
    ) -> tuple[
        tuple[
            str,
            str,
            tuple[tuple[str, str], ...],
            str,
            str,
            str,
        ],
        ...,
    ]:
        signature = []
        for table in sorted(_EXPECTED_SCHEMA_COLUMNS):
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall():
                grouped.setdefault(int(row["id"]), []).append(row)
            for rows in grouped.values():
                ordered = sorted(rows, key=lambda row: int(row["seq"]))
                if [int(row["seq"]) for row in ordered] != list(
                    range(len(ordered))
                ):
                    raise RunDatabaseError(
                        "durable run catalog composite foreign key is malformed"
                    )
                parent = str(ordered[0]["table"])
                on_update = str(ordered[0]["on_update"])
                on_delete = str(ordered[0]["on_delete"])
                match = str(ordered[0]["match"])
                if any(
                    row["table"] != parent
                    or row["on_update"] != on_update
                    or row["on_delete"] != on_delete
                    or row["match"] != match
                    for row in ordered
                ):
                    raise RunDatabaseError(
                        "durable run catalog composite foreign key is incoherent"
                    )
                signature.append(
                    (
                        table,
                        parent,
                        tuple((str(row["from"]), str(row["to"])) for row in ordered),
                        on_update,
                        on_delete,
                        match,
                    )
                )
        return tuple(sorted(signature))

    @classmethod
    def _expected_schema_signature(
        cls, migration_statements: tuple[str, ...]
    ) -> tuple[tuple[str, str, str, str | None], ...]:
        with sqlite3.connect(":memory:") as reference:
            for statement in migration_statements:
                reference.execute(statement)
            return cls._schema_signature(reference)

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection,
        *,
        expected_signature: tuple[tuple[str, str, str, str | None], ...],
    ) -> None:
        if RunRepository._schema_signature(connection) != expected_signature:
            raise RunDatabaseError(
                "durable run catalog SQL schema does not match packaged v1"
            )
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        expected_tables = {
            name
            for object_type, name, _owner, _sql in expected_signature
            if object_type == "table"
        }
        if tables != expected_tables:
            raise RunDatabaseError("durable run catalog table shape is incomplete")
        for table, expected_columns in _EXPECTED_SCHEMA_COLUMNS.items():
            columns = {
                row["name"]
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if columns != expected_columns:
                raise RunDatabaseError(
                    f"durable run catalog columns are malformed for {table}"
                )
        indexes = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND sql IS NOT NULL
                """
            ).fetchall()
        }
        if indexes != _EXPECTED_SCHEMA_INDEXES:
            raise RunDatabaseError("durable run catalog index shape is incomplete")
        for index_name, (
            expected_table,
            expected_columns,
            expected_unique,
            expected_partial,
        ) in _EXPECTED_INDEX_SHAPES.items():
            index_row = connection.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            if index_row is None or index_row["tbl_name"] != expected_table:
                raise RunDatabaseError("durable run catalog index owner is malformed")
            columns = tuple(
                row["name"]
                for row in connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
            )
            flags = connection.execute(
                f'PRAGMA index_list("{expected_table}")'
            ).fetchall()
            flag_row = next((row for row in flags if row["name"] == index_name), None)
            if (
                columns != expected_columns
                or flag_row is None
                or flag_row["unique"] != expected_unique
                or flag_row["partial"] != expected_partial
            ):
                raise RunDatabaseError("durable run catalog index shape is malformed")
        if (
            RunRepository._foreign_key_signature(connection)
            != _EXPECTED_FOREIGN_KEY_GROUPS
        ):
            raise RunDatabaseError("durable run catalog foreign-key shape is malformed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RunDatabaseError("durable run catalog contains foreign-key violations")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RunDatabaseError("durable run catalog integrity check failed")
        integrity_results = tuple(
            str(row[0])
            for row in connection.execute("PRAGMA integrity_check").fetchall()
        )
        if integrity_results != ("ok",):
            raise RunDatabaseError("durable run catalog integrity check failed")

    @staticmethod
    def _validate_relational_invariants(connection: sqlite3.Connection) -> None:
        heads = tuple(
            RunRepository._book_head_from_row(row)
            for row in connection.execute("SELECT * FROM book_heads").fetchall()
        )
        runs = tuple(
            RunRepository._run_from_row(row)
            for row in connection.execute("SELECT * FROM snapshot_runs").fetchall()
        )
        publications = tuple(
            RunRepository._publication_from_row(row)
            for row in connection.execute("SELECT * FROM snapshot_manifests").fetchall()
        )
        pointer_rows = connection.execute(
            """
            SELECT active.*,
                   publication.snapshot_id AS _bound_snapshot_id,
                   publication.book_id AS _bound_book_id,
                   publication.book_generation AS _bound_book_generation,
                   publication.snapshot_status AS _bound_snapshot_status,
                   publication.publication_sequence AS _bound_publication_sequence,
                   publication.published_at_utc AS _bound_published_at_utc
            FROM active_snapshots AS active
            LEFT JOIN snapshot_manifests AS publication
              ON publication.book_id = active.book_id
             AND publication.snapshot_id = active.snapshot_id
             AND publication.book_generation = active.book_generation
            """
        ).fetchall()
        pointer_registers = tuple(
            RunRepository._pointer_register_from_row(row) for row in pointer_rows
        )
        recovery_events = tuple(
            RunRepository._recovery_event_from_row(row)
            for row in connection.execute(
                "SELECT * FROM snapshot_recovery_events"
            ).fetchall()
        )
        completed_tails: set[tuple[str, str]] = set()

        for run in runs:
            RunRepository._validate_run_relations(
                connection,
                run,
                completed_tails=completed_tails,
            )
        for publication in publications:
            RunRepository._validate_publication_relations(
                connection,
                publication,
                completed_tails=completed_tails,
            )
        for head in heads:
            RunRepository._validate_head_relations(connection, head)
        for register in pointer_registers:
            RunRepository._validate_pointer_register_relations(
                connection,
                register,
                completed_tails=completed_tails,
            )
            RunRepository._validate_pointer_history(connection, register)
        for event in recovery_events:
            RunRepository._validate_recovery_event_relations(
                connection,
                event,
                completed_tails=completed_tails,
            )

        invariant_queries = (
            (
                "durable run ownership or expected-pointer provenance is malformed",
                """
                SELECT 1
                FROM snapshot_runs AS run
                LEFT JOIN book_heads AS head ON head.book_id = run.book_id
                LEFT JOIN snapshot_manifests AS expected
                  ON expected.book_id = run.book_id
                 AND expected.snapshot_id = run.expected_active_snapshot_id
                LEFT JOIN snapshot_manifests AS owned
                  ON owned.run_id = run.run_id
                WHERE
                    (
                        run.book_id IS NULL
                        AND (
                            run.captured_generation IS NOT NULL
                            OR run.target_cut_utc IS NOT NULL
                            OR run.expected_active_snapshot_id IS NOT NULL
                            OR run.expected_active_pointer_version <> 0
                            OR run.candidate_snapshot_id IS NOT NULL
                            OR run.published_snapshot_id IS NOT NULL
                        )
                    )
                    OR (
                        run.book_id IS NOT NULL
                        AND (
                            run.captured_generation IS NULL
                            OR run.target_cut_utc IS NULL
                            OR head.book_id IS NULL
                            OR run.captured_generation > head.generation
                        )
                    )
                    OR (
                        run.expected_active_snapshot_id IS NOT NULL
                        AND (
                            run.expected_active_pointer_version < 1
                            OR expected.snapshot_id IS NULL
                            OR expected.book_generation > run.captured_generation
                            OR expected.published_at_utc > run.requested_at_utc
                        )
                    )
                    OR (
                        run.run_outcome = 'SUCCEEDED'
                        AND run.book_id IS NOT NULL
                        AND (
                            run.run_stage <> 'PUBLISHING'
                            OR run.candidate_snapshot_id IS NULL
                            OR run.published_snapshot_id IS NULL
                            OR run.candidate_snapshot_id <> run.published_snapshot_id
                            OR owned.run_id IS NULL
                            OR owned.book_id <> run.book_id
                            OR owned.book_generation <> run.captured_generation
                            OR owned.snapshot_id <> run.candidate_snapshot_id
                            OR owned.published_at_utc <> run.finished_at_utc
                            OR owned.published_at_utc <> run.updated_at_utc
                        )
                    )
                LIMIT 1
                """,
            ),
            (
                "publication provenance does not match its durable run",
                """
                SELECT 1
                FROM snapshot_manifests AS publication
                LEFT JOIN snapshot_runs AS run ON run.run_id = publication.run_id
                WHERE run.run_id IS NULL
                   OR run.book_id IS NULL
                   OR run.run_outcome <> 'SUCCEEDED'
                   OR run.run_stage <> 'PUBLISHING'
                   OR publication.book_id <> run.book_id
                   OR publication.book_generation <> run.captured_generation
                   OR publication.snapshot_id <> run.candidate_snapshot_id
                   OR publication.snapshot_id <> run.published_snapshot_id
                   OR publication.published_at_utc <> run.finished_at_utc
                   OR publication.published_at_utc <> run.updated_at_utc
                LIMIT 1
                """,
            ),
            (
                "active pointer register ownership is malformed",
                """
                SELECT 1
                FROM book_heads AS head
                LEFT JOIN active_snapshots AS pointer
                  ON pointer.book_id = head.book_id
                WHERE pointer.book_id IS NULL
                UNION ALL
                SELECT 1
                FROM active_snapshots AS pointer
                LEFT JOIN book_heads AS head
                  ON head.book_id = pointer.book_id
                WHERE head.book_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "active snapshot provenance is malformed",
                """
                SELECT 1
                FROM active_snapshots AS active
                LEFT JOIN book_heads AS head ON head.book_id = active.book_id
                LEFT JOIN snapshot_manifests AS publication
                  ON publication.book_id = active.book_id
                 AND publication.snapshot_id = active.snapshot_id
                 AND publication.book_generation = active.book_generation
                WHERE head.book_id IS NULL
                   OR (active.snapshot_id IS NULL) <> (active.book_generation IS NULL)
                   OR (
                       active.snapshot_id IS NOT NULL
                       AND (
                           publication.snapshot_id IS NULL
                           OR active.book_generation > head.generation
                           OR active.updated_at_utc < publication.published_at_utc
                       )
                   )
                LIMIT 1
                """,
            ),
            (
                "recovery evidence provenance is malformed",
                """
                SELECT 1
                FROM snapshot_recovery_events AS event
                LEFT JOIN snapshot_manifests AS rejected
                  ON rejected.book_id = event.book_id
                 AND rejected.snapshot_id = event.rejected_snapshot_id
                LEFT JOIN snapshot_manifests AS selected
                  ON selected.book_id = event.book_id
                 AND selected.snapshot_id = event.selected_snapshot_id
                WHERE rejected.snapshot_id IS NULL
                   OR (
                       event.selected_snapshot_id IS NOT NULL
                       AND selected.snapshot_id IS NULL
                   )
                   OR (
                       event.selected_snapshot_id IS NOT NULL
                       AND (
                           event.selected_snapshot_id = event.rejected_snapshot_id
                           OR selected.snapshot_status <> 'BLESSED'
                       )
                   )
                   OR (
                       event.resolution_action = 'REPOINTED'
                       AND event.selected_snapshot_id IS NULL
                   )
                   OR (
                       event.resolution_action = 'REMOVED'
                       AND event.selected_snapshot_id IS NOT NULL
                   )
                   OR event.resolution_action NOT IN ('REPOINTED', 'REMOVED', 'CAS_LOST')
                LIMIT 1
                """,
            ),
        )
        for message, query in invariant_queries:
            if connection.execute(query).fetchone() is not None:
                raise RunDatabaseError(message)

    def audit_integrity(self) -> None:
        """Run the complete schema, scalar, lifecycle, and relational audit on demand."""

        migration = (
            resources.files("quantmind.snapshots.migrations")
            .joinpath("0001_run_catalog.sql")
            .read_text(encoding="utf-8")
        )
        expected_signature = self._expected_schema_signature(
            self._migration_statements(migration)
        )
        with self._read_connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _CURRENT_SCHEMA_VERSION:
                raise RunDatabaseError(
                    f"unsupported durable run schema version: {version}"
                )
            self._validate_schema(
                connection, expected_signature=expected_signature
            )
            self._validate_relational_invariants(connection)

    def inspect_connection_pragmas(self) -> ConnectionPragmasV1:
        with self._read_connection() as connection:
            return ConnectionPragmasV1(
                foreign_keys=int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                journal_mode=str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower(),
                synchronous=int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                busy_timeout_ms=int(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0]
                ),
            )

    @staticmethod
    def _book_head_from_row(row: sqlite3.Row) -> BookHeadV1:
        try:
            return BookHeadV1(
                book_id=row["book_id"],
                generation=row["generation"],
                canonical_book_ref=row["canonical_book_ref"],
                updated_at_utc=_parse_timestamp(row["updated_at_utc"]),
                version=row["version"],
            )
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("durable book-head row violates the v1 schema") from error

    @staticmethod
    def _decode_run_row(row: sqlite3.Row) -> RunRecordV1:
        result_json = row["result_json"]
        result = None
        if result_json is not None:
            parsed_result = _validate_canonical_json_text(
                result_json, maximum_bytes=_MAX_RESULT_BYTES
            )
            result = RunResultV1.model_validate_json(
                canonical_json_bytes(parsed_result)
            )
        return RunRecordV1(
            run_id=row["run_id"],
            run_kind=row["run_kind"],
            idempotency_identity=row["idempotency_identity"],
            request_fingerprint=row["request_fingerprint"],
            client_idempotency_key_digest=row["client_idempotency_key_digest"],
            book_id=row["book_id"],
            captured_generation=row["captured_generation"],
            expected_active_snapshot_id=row["expected_active_snapshot_id"],
            expected_active_pointer_version=row["expected_active_pointer_version"],
            target_cut_utc=_parse_timestamp(row["target_cut_utc"]),
            requested_at_utc=_parse_timestamp(row["requested_at_utc"]),
            started_at_utc=_parse_timestamp(row["started_at_utc"]),
            updated_at_utc=_parse_timestamp(row["updated_at_utc"]),
            finished_at_utc=_parse_timestamp(row["finished_at_utc"]),
            run_stage=RunStage(row["run_stage"]),
            run_outcome=RunOutcome(row["run_outcome"]),
            cancel_requested_at_utc=_parse_timestamp(
                row["cancel_requested_at_utc"]
            ),
            candidate_snapshot_id=row["candidate_snapshot_id"],
            published_snapshot_id=row["published_snapshot_id"],
            result=result,
            error_code=(
                None if row["error_code"] is None else RunErrorCode(row["error_code"])
            ),
            error_message=row["error_message"],
            version=row["version"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecordV1:
        try:
            record = RunRepository._decode_run_row(row)
            expected_identity = _idempotency_identity_from_preimage(
                run_kind=record.run_kind,
                request_fingerprint=record.request_fingerprint,
                client_idempotency_key_digest=record.client_idempotency_key_digest,
                book_id=record.book_id,
                captured_generation=record.captured_generation,
                target_cut_utc=record.target_cut_utc,
            )
            if record.idempotency_identity != expected_identity:
                raise ValueError(
                    "stored idempotency identity does not match its canonical preimage"
                )
            return record
        except RunRepositoryError:
            raise
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("durable run row violates the v1 schema") from error

    @staticmethod
    def _publication_from_row(row: sqlite3.Row) -> ManifestPublicationRecordV1:
        try:
            return ManifestPublicationRecordV1(
                publication_sequence=row["publication_sequence"],
                snapshot_id=row["snapshot_id"],
                run_id=row["run_id"],
                book_id=row["book_id"],
                book_generation=row["book_generation"],
                snapshot_status=SnapshotStatus(row["snapshot_status"]),
                schema_version=row["schema_version"],
                hash_algorithm=row["hash_algorithm"],
                manifest_relpath=row["manifest_relpath"],
                envelope_sha256=row["envelope_sha256"],
                envelope_byte_length=row["envelope_byte_length"],
                published_at_utc=_parse_timestamp(row["published_at_utc"]),
            )
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("publication row violates the v1 schema") from error

    @staticmethod
    def _pointer_register_from_row(row: sqlite3.Row) -> _ActivePointerRegisterV1:
        try:
            register = _ActivePointerRegisterV1(
                book_id=row["book_id"],
                snapshot_id=row["snapshot_id"],
                book_generation=row["book_generation"],
                pointer_version=row["pointer_version"],
                updated_at_utc=_parse_timestamp(row["updated_at_utc"]),
            )
            if register.snapshot_id is None:
                if any(
                    row[column] is not None
                    for column in (
                        "_bound_snapshot_id",
                        "_bound_book_id",
                        "_bound_book_generation",
                        "_bound_snapshot_status",
                        "_bound_publication_sequence",
                        "_bound_published_at_utc",
                    )
                ):
                    raise ValueError("pointer tombstone unexpectedly binds a publication")
            elif (
                row["_bound_snapshot_id"] != register.snapshot_id
                or row["_bound_book_id"] != register.book_id
                or row["_bound_book_generation"] != register.book_generation
            ):
                raise ValueError("active pointer is not bound to one publication")
            else:
                SnapshotStatus(row["_bound_snapshot_status"])
            return register
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("active pointer row violates the v1 schema") from error

    @staticmethod
    def _active_from_row(row: sqlite3.Row) -> ActiveSnapshotV1:
        active = RunRepository._pointer_register_from_row(row).active_snapshot()
        if active is None:
            raise RunDatabaseError("active pointer row is a tombstone")
        return active

    @staticmethod
    def _recovery_event_from_row(row: sqlite3.Row) -> RecoveryEventV1:
        try:
            return RecoveryEventV1(
                event_sequence=row["event_sequence"],
                book_id=row["book_id"],
                rejected_snapshot_id=row["rejected_snapshot_id"],
                expected_pointer_version=row["expected_pointer_version"],
                resolution_action=ActiveRecoveryDecision(row["resolution_action"]),
                selected_snapshot_id=row["selected_snapshot_id"],
                detail_json=row["detail_json"],
                recorded_at_utc=_parse_timestamp(row["recorded_at_utc"]),
            )
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("recovery event row violates the v1 schema") from error

    @staticmethod
    def _pointer_transition_from_row(row: sqlite3.Row) -> _PointerTransitionV1:
        try:
            return _PointerTransitionV1(
                book_id=row["book_id"],
                previous_snapshot_id=row["previous_snapshot_id"],
                selected_snapshot_id=row["selected_snapshot_id"],
                pointer_version=row["pointer_version"],
                transitioned_at_utc=_parse_timestamp(row["transitioned_at_utc"]),
                transition_kind=row["transition_kind"],
            )
        except (LookupError, TypeError, ValueError) as error:
            raise RunDatabaseError("active pointer transition is malformed") from error

    @staticmethod
    def _pointer_transition_rows(
        connection: sqlite3.Connection,
        book_id: str,
        *,
        pointer_version: int | None = None,
    ) -> tuple[sqlite3.Row, ...]:
        version_clause = (
            "" if pointer_version is None else " AND run.expected_active_pointer_version = ?"
        )
        recovery_version_clause = (
            "" if pointer_version is None else " AND event.expected_pointer_version = ?"
        )
        limit_clause = "" if pointer_version is None else " LIMIT 2"
        parameters: tuple[object, ...]
        if pointer_version is None:
            parameters = (book_id, book_id)
        else:
            previous_version = pointer_version - 1
            parameters = (book_id, previous_version, book_id, previous_version)
        return tuple(
            connection.execute(
                f"""
                SELECT run.book_id AS book_id,
                       run.expected_active_snapshot_id AS previous_snapshot_id,
                       publication.snapshot_id AS selected_snapshot_id,
                       run.expected_active_pointer_version + 1 AS pointer_version,
                       publication.published_at_utc AS transitioned_at_utc,
                       'PUBLICATION' AS transition_kind
                FROM snapshot_runs AS run
                JOIN snapshot_manifests AS publication
                  ON publication.run_id = run.run_id
                WHERE run.book_id = ?{version_clause}
                  AND run.run_outcome = 'SUCCEEDED'
                  AND run.published_snapshot_id IS NOT NULL
                UNION ALL
                SELECT event.book_id AS book_id,
                       event.rejected_snapshot_id AS previous_snapshot_id,
                       event.selected_snapshot_id AS selected_snapshot_id,
                       event.expected_pointer_version + 1 AS pointer_version,
                       event.recorded_at_utc AS transitioned_at_utc,
                       event.resolution_action AS transition_kind
                FROM snapshot_recovery_events AS event
                WHERE event.book_id = ?
                  AND event.resolution_action IN ('REPOINTED', 'REMOVED')
                  {recovery_version_clause}
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        )

    @staticmethod
    def _pointer_transition_at_version(
        connection: sqlite3.Connection,
        book_id: str,
        pointer_version: int,
    ) -> _PointerTransitionV1:
        if pointer_version < 1:
            raise RunDatabaseError("active pointer transition version is invalid")
        rows = RunRepository._pointer_transition_rows(
            connection,
            book_id,
            pointer_version=pointer_version,
        )
        if len(rows) != 1:
            raise RunDatabaseError("active pointer transition history is not unique")
        transition = RunRepository._pointer_transition_from_row(rows[0])
        if transition.pointer_version != pointer_version:
            raise RunDatabaseError("active pointer transition version is malformed")
        return transition

    @staticmethod
    def _has_pointer_transition_after(
        connection: sqlite3.Connection,
        book_id: str,
        pointer_version: int,
    ) -> bool:
        return connection.execute(
            """
            SELECT 1
            FROM (
                SELECT run.expected_active_pointer_version AS _sort_version
                FROM snapshot_runs AS run
                WHERE run.book_id = ?
                  AND run.expected_active_pointer_version >= ?
                  AND run.run_outcome = 'SUCCEEDED'
                  AND run.published_snapshot_id IS NOT NULL
                UNION ALL
                SELECT event.expected_pointer_version AS _sort_version
                FROM snapshot_recovery_events AS event
                WHERE event.book_id = ?
                  AND event.expected_pointer_version >= ?
                  AND event.resolution_action IN ('REPOINTED', 'REMOVED')
            )
            LIMIT 1
            """,
            (book_id, pointer_version, book_id, pointer_version),
        ).fetchone() is not None

    @staticmethod
    def _ordered_pointer_transition_rows(
        connection: sqlite3.Connection,
        book_id: str,
    ) -> Iterator[sqlite3.Row]:
        yield from connection.execute(
            """
            SELECT run.book_id AS book_id,
                   run.expected_active_snapshot_id AS previous_snapshot_id,
                   publication.snapshot_id AS selected_snapshot_id,
                   run.expected_active_pointer_version + 1 AS pointer_version,
                   publication.published_at_utc AS transitioned_at_utc,
                   'PUBLICATION' AS transition_kind,
                   run.expected_active_pointer_version AS _sort_version
            FROM snapshot_runs AS run
            JOIN snapshot_manifests AS publication
              ON publication.run_id = run.run_id
            WHERE run.book_id = ?
              AND run.run_outcome = 'SUCCEEDED'
              AND run.published_snapshot_id IS NOT NULL
            UNION ALL
            SELECT event.book_id AS book_id,
                   event.rejected_snapshot_id AS previous_snapshot_id,
                   event.selected_snapshot_id AS selected_snapshot_id,
                   event.expected_pointer_version + 1 AS pointer_version,
                   event.recorded_at_utc AS transitioned_at_utc,
                   event.resolution_action AS transition_kind,
                   event.expected_pointer_version AS _sort_version
            FROM snapshot_recovery_events AS event
            WHERE event.book_id = ?
              AND event.resolution_action IN ('REPOINTED', 'REMOVED')
            ORDER BY _sort_version
            """,
            (book_id, book_id),
        )

    @staticmethod
    def _validate_pointer_register_relations(
        connection: sqlite3.Connection,
        register: _ActivePointerRegisterV1,
        *,
        completed_tails: set[tuple[str, str]] | None = None,
    ) -> None:
        head_row = connection.execute(
            "SELECT * FROM book_heads WHERE book_id = ?", (register.book_id,)
        ).fetchone()
        if head_row is None:
            raise RunDatabaseError("active pointer register has no book head")
        head = RunRepository._book_head_from_row(head_row)
        active = register.active_snapshot()
        if active is not None:
            if active.book_generation > head.generation:
                raise RunDatabaseError("book head is older than its active snapshot")
            RunRepository._validate_active_binding(connection, active)
        if RunRepository._has_pointer_transition_after(
            connection,
            register.book_id,
            register.pointer_version,
        ):
            raise RunDatabaseError(
                "active pointer register is behind durable transition history"
            )
        if register.pointer_version == 0:
            if active is not None or register.updated_at_utc > head.updated_at_utc:
                raise RunDatabaseError("virgin active pointer register is malformed")
            return
        transition = RunRepository._pointer_transition_at_version(
            connection,
            register.book_id,
            register.pointer_version,
        )
        if register.pointer_version == 1:
            if transition.previous_snapshot_id is not None:
                raise RunDatabaseError(
                    "first active pointer transition has a predecessor"
                )
        else:
            predecessor = RunRepository._pointer_transition_at_version(
                connection,
                register.book_id,
                register.pointer_version - 1,
            )
            if (
                transition.previous_snapshot_id
                != predecessor.selected_snapshot_id
                or transition.transitioned_at_utc < predecessor.transitioned_at_utc
            ):
                raise RunDatabaseError(
                    "active pointer tail is not causally linked to its predecessor"
                )
        if (
            transition.selected_snapshot_id != register.snapshot_id
            or transition.transitioned_at_utc != register.updated_at_utc
        ):
            raise RunDatabaseError("active pointer register does not match its transition")

    @staticmethod
    def _validate_pointer_history(
        connection: sqlite3.Connection,
        register: _ActivePointerRegisterV1,
    ) -> None:
        expected_pointer_version = 1
        previous_snapshot_id: str | None = None
        previous_time: datetime | None = None
        transition_count = 0
        for row in RunRepository._ordered_pointer_transition_rows(
            connection,
            register.book_id,
        ):
            transition = RunRepository._pointer_transition_from_row(row)
            if transition.pointer_version < expected_pointer_version:
                raise RunDatabaseError("active pointer transition history is not unique")
            if transition.pointer_version > expected_pointer_version:
                raise RunDatabaseError(
                    "active pointer transition history is not contiguous"
                )
            if transition.previous_snapshot_id != previous_snapshot_id:
                raise RunDatabaseError("active pointer transition predecessor is malformed")
            if (
                previous_time is not None
                and transition.transitioned_at_utc < previous_time
            ):
                raise RunDatabaseError("active pointer transition clock moved backward")
            previous_snapshot_id = transition.selected_snapshot_id
            previous_time = transition.transitioned_at_utc
            transition_count += 1
            expected_pointer_version += 1

        if transition_count != register.pointer_version:
            raise RunDatabaseError("active pointer transition history is not contiguous")

        if register.pointer_version == 0:
            if register.snapshot_id is not None:
                raise RunDatabaseError("virgin active pointer register is malformed")
        elif (
            previous_snapshot_id != register.snapshot_id
            or previous_time != register.updated_at_utc
        ):
            raise RunDatabaseError("active pointer register is not the history tail")

    @staticmethod
    def _validate_head_relations(
        connection: sqlite3.Connection, head: BookHeadV1
    ) -> None:
        if connection.execute(
            """
            SELECT 1 FROM snapshot_runs
            WHERE book_id = ? AND captured_generation > ? LIMIT 1
            """,
            (head.book_id, head.generation),
        ).fetchone() is not None:
            raise RunDatabaseError("book head is older than durable run history")
        if connection.execute(
            """
            SELECT 1 FROM snapshot_manifests
            WHERE book_id = ? AND book_generation > ? LIMIT 1
            """,
            (head.book_id, head.generation),
        ).fetchone() is not None:
            raise RunDatabaseError("book head is older than publication history")
        pointer_row = RunRepository._pointer_row(connection, head.book_id)
        if pointer_row is None:
            raise RunDatabaseError("book head has no active pointer register")
        register = RunRepository._pointer_register_from_row(pointer_row)
        RunRepository._validate_pointer_register_relations(connection, register)

    @staticmethod
    def _validate_run_head_relation(
        connection: sqlite3.Connection, record: RunRecordV1
    ) -> None:
        if record.book_id is not None:
            head_row = connection.execute(
                "SELECT * FROM book_heads WHERE book_id = ?", (record.book_id,)
            ).fetchone()
            if head_row is None:
                raise RunDatabaseError("durable run has no canonical book head")
            head = RunRepository._book_head_from_row(head_row)
            if record.captured_generation is None or (
                record.captured_generation > head.generation
            ):
                raise RunDatabaseError("durable run generation exceeds its book head")
            RunRepository._validate_stale_generation_causality(record, head)

    @staticmethod
    def _validate_stale_generation_causality(
        record: RunRecordV1,
        head: BookHeadV1,
    ) -> None:
        """Validate stale-generation evidence available in the v1 schema.

        ``book_heads`` retains only the current head, not transition history. Its
        timestamp therefore supplies only a necessary retained-head condition; v1
        cannot attest the exact transition that first invalidated the run.
        """

        if record.error_code is not RunErrorCode.STALE_BOOK_GENERATION:
            return
        if (
            record.captured_generation is None
            or head.generation <= record.captured_generation
        ):
            raise RunDatabaseError(
                "stale book generation lacks a newer canonical head"
            )
        if head.updated_at_utc < record.requested_at_utc:
            raise RunDatabaseError(
                "stale book generation head predates its run request"
            )

    @staticmethod
    def _validate_stale_pointer_causality(
        record: RunRecordV1,
        invalidating: _PointerTransitionV1,
    ) -> None:
        if (
            invalidating.previous_snapshot_id
            != record.expected_active_snapshot_id
            or invalidating.transitioned_at_utc < record.requested_at_utc
            or record.finished_at_utc is None
            or record.finished_at_utc < invalidating.transitioned_at_utc
        ):
            raise RunDatabaseError("stale pointer terminal evidence is not causal")

    @staticmethod
    def _expected_publication_for_run(
        connection: sqlite3.Connection, record: RunRecordV1
    ) -> ManifestPublicationRecordV1 | None:
        if record.expected_active_snapshot_id is None:
            return None
        expected_row = connection.execute(
            """
            SELECT * FROM snapshot_manifests
            WHERE book_id = ? AND snapshot_id = ?
            """,
            (record.book_id, record.expected_active_snapshot_id),
        ).fetchone()
        if expected_row is None:
            raise RunDatabaseError("expected active publication is missing")
        expected = RunRepository._publication_from_row(expected_row)
        if (
            record.captured_generation is None
            or expected.book_generation > record.captured_generation
            or expected.published_at_utc > record.requested_at_utc
        ):
            raise RunDatabaseError("expected active publication postdates its run")
        return expected

    @staticmethod
    def _validate_publication_owner_tuple(
        publication: ManifestPublicationRecordV1,
        owner: RunRecordV1,
    ) -> None:
        if (
            owner.book_id != publication.book_id
            or owner.captured_generation != publication.book_generation
            or owner.run_outcome is not RunOutcome.SUCCEEDED
            or owner.run_stage is not RunStage.PUBLISHING
            or owner.candidate_snapshot_id != publication.snapshot_id
            or owner.published_snapshot_id != publication.snapshot_id
            or owner.finished_at_utc != publication.published_at_utc
            or owner.updated_at_utc != publication.published_at_utc
        ):
            raise RunDatabaseError("publication provenance does not match its run")

    @staticmethod
    def _publication_owner(
        connection: sqlite3.Connection,
        publication: ManifestPublicationRecordV1,
    ) -> RunRecordV1:
        run_row = connection.execute(
            "SELECT * FROM snapshot_runs WHERE run_id = ?", (publication.run_id,)
        ).fetchone()
        if run_row is None:
            raise RunDatabaseError("publication owner run is missing")
        owner = RunRepository._run_from_row(run_row)
        RunRepository._validate_run_head_relation(connection, owner)
        RunRepository._validate_publication_owner_tuple(publication, owner)
        return owner

    @staticmethod
    def _validate_run_relations(
        connection: sqlite3.Connection,
        record: RunRecordV1,
        *,
        completed_tails: set[tuple[str, str]] | None = None,
    ) -> None:
        RunRepository._validate_run_head_relation(connection, record)
        owned_row = connection.execute(
            "SELECT * FROM snapshot_manifests WHERE run_id = ?", (record.run_id,)
        ).fetchone()
        if record.run_outcome is RunOutcome.SUCCEEDED and record.book_id is not None:
            if owned_row is None:
                raise RunDatabaseError("successful book run has no publication")
            publication = RunRepository._publication_from_row(owned_row)
            RunRepository._validate_publication_owner_tuple(publication, record)
            RunRepository._validate_publication_relations(
                connection,
                publication,
                completed_tails=completed_tails,
            )
        elif owned_row is not None:
            raise RunDatabaseError("non-successful run unexpectedly owns a publication")
        else:
            expected = RunRepository._expected_publication_for_run(connection, record)
            if expected is not None:
                RunRepository._validate_publication_relations(
                    connection,
                    expected,
                    completed_tails=completed_tails,
                )

        if record.book_id is not None:
            expected_version = record.expected_active_pointer_version
            if expected_version == 0:
                if record.expected_active_snapshot_id is not None:
                    raise RunDatabaseError("virgin pointer capture is malformed")
            else:
                captured_pointer = RunRepository._pointer_transition_at_version(
                    connection,
                    record.book_id,
                    expected_version,
                )
                if (
                    captured_pointer.selected_snapshot_id
                    != record.expected_active_snapshot_id
                    or captured_pointer.transitioned_at_utc > record.requested_at_utc
                ):
                    raise RunDatabaseError("run pointer capture is not causal")
            if record.error_code is RunErrorCode.STALE_ACTIVE_POINTER:
                invalidating = RunRepository._pointer_transition_at_version(
                    connection,
                    record.book_id,
                    expected_version + 1,
                )
                RunRepository._validate_stale_pointer_causality(
                    record,
                    invalidating,
                )

    @staticmethod
    def _validate_publication_relations(
        connection: sqlite3.Connection,
        publication: ManifestPublicationRecordV1,
        *,
        completed_tails: set[tuple[str, str]] | None = None,
    ) -> None:
        completed = set() if completed_tails is None else completed_tails
        seen: set[tuple[str, str]] = set()
        path: list[tuple[str, str]] = []
        sequence_is_historical = True
        current = publication
        while True:
            identity = (current.book_id, current.snapshot_id)
            if identity in completed:
                if not sequence_is_historical:
                    raise RunDatabaseError(
                        "expected-active publication sequence is not historical"
                    )
                completed.update(path)
                return
            if identity in seen:
                raise RunDatabaseError(
                    "expected-active publication provenance contains a cycle"
                )
            seen.add(identity)
            path.append(identity)
            owner = RunRepository._publication_owner(connection, current)
            expected = RunRepository._expected_publication_for_run(
                connection, owner
            )
            if expected is None:
                if not sequence_is_historical:
                    raise RunDatabaseError(
                        "expected-active publication sequence is not historical"
                    )
                completed.update(path)
                return
            expected_identity = (expected.book_id, expected.snapshot_id)
            if expected_identity in seen:
                raise RunDatabaseError(
                    "expected-active publication provenance contains a cycle"
                )
            if expected.publication_sequence >= current.publication_sequence:
                sequence_is_historical = False
            current = expected

    @staticmethod
    def _validate_active_relations(
        connection: sqlite3.Connection,
        active: ActiveSnapshotV1,
        *,
        completed_tails: set[tuple[str, str]] | None = None,
    ) -> ManifestPublicationRecordV1:
        publication = RunRepository._validate_active_binding(connection, active)
        RunRepository._validate_publication_relations(
            connection,
            publication,
            completed_tails=completed_tails,
        )
        return publication

    @staticmethod
    def _validate_active_binding(
        connection: sqlite3.Connection,
        active: ActiveSnapshotV1,
    ) -> ManifestPublicationRecordV1:
        publication_row = connection.execute(
            """
            SELECT * FROM snapshot_manifests
            WHERE book_id = ? AND snapshot_id = ? AND book_generation = ?
            """,
            (active.book_id, active.snapshot_id, active.book_generation),
        ).fetchone()
        if publication_row is None:
            raise RunDatabaseError("active publication is missing")
        publication = RunRepository._publication_from_row(publication_row)
        RunRepository._publication_owner(connection, publication)
        head_row = connection.execute(
            "SELECT * FROM book_heads WHERE book_id = ?", (active.book_id,)
        ).fetchone()
        if head_row is None:
            raise RunDatabaseError("active snapshot book head is missing")
        head = RunRepository._book_head_from_row(head_row)
        if (
            active.book_generation > head.generation
            or active.updated_at_utc < publication.published_at_utc
        ):
            raise RunDatabaseError("active snapshot provenance is malformed")
        return publication

    @staticmethod
    def _validate_recovery_event_relations(
        connection: sqlite3.Connection,
        event: RecoveryEventV1,
        *,
        completed_tails: set[tuple[str, str]] | None = None,
    ) -> None:
        rejected = connection.execute(
            """
            SELECT * FROM snapshot_manifests
            WHERE book_id = ? AND snapshot_id = ?
            """,
            (event.book_id, event.rejected_snapshot_id),
        ).fetchone()
        if rejected is None:
            raise RunDatabaseError("recovery rejected publication is missing")
        rejected_publication = RunRepository._publication_from_row(rejected)
        RunRepository._validate_publication_relations(
            connection,
            rejected_publication,
            completed_tails=completed_tails,
        )
        if event.recorded_at_utc < rejected_publication.published_at_utc:
            raise RunDatabaseError("recovery evidence predates rejected publication")
        if event.selected_snapshot_id is not None:
            selected = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ? AND snapshot_id = ?
                """,
                (event.book_id, event.selected_snapshot_id),
            ).fetchone()
            if selected is None:
                raise RunDatabaseError("recovery selected publication is missing")
            publication = RunRepository._publication_from_row(selected)
            RunRepository._validate_publication_relations(
                connection,
                publication,
                completed_tails=completed_tails,
            )
            if (
                publication.snapshot_status is not SnapshotStatus.BLESSED
                or event.selected_snapshot_id == event.rejected_snapshot_id
                or event.recorded_at_utc < publication.published_at_utc
            ):
                raise RunDatabaseError("recovery selected publication is not safe")

        predecessor = RunRepository._pointer_transition_at_version(
            connection,
            event.book_id,
            event.expected_pointer_version,
        )
        if predecessor.selected_snapshot_id != event.rejected_snapshot_id:
            raise RunDatabaseError("recovery evidence rejected the wrong pointer revision")
        invalidating = RunRepository._pointer_transition_at_version(
            connection,
            event.book_id,
            event.expected_pointer_version + 1,
        )
        if (
            invalidating.previous_snapshot_id != event.rejected_snapshot_id
            or invalidating.transitioned_at_utc > event.recorded_at_utc
        ):
            raise RunDatabaseError("recovery evidence predates its pointer transition")
        if event.resolution_action in {
            ActiveRecoveryDecision.REPOINTED,
            ActiveRecoveryDecision.REMOVED,
        } and (
            invalidating.transition_kind != event.resolution_action.value
            or invalidating.selected_snapshot_id != event.selected_snapshot_id
            or invalidating.transitioned_at_utc != event.recorded_at_utc
        ):
            raise RunDatabaseError("recovery transition evidence is malformed")

        previous_event = connection.execute(
            """
            SELECT recorded_at_utc
            FROM snapshot_recovery_events
            WHERE book_id = ? AND event_sequence < ?
            ORDER BY event_sequence DESC
            LIMIT 1
            """,
            (event.book_id, event.event_sequence),
        ).fetchone()
        if (
            previous_event is not None
            and previous_event["recorded_at_utc"]
            > _timestamp_text(event.recorded_at_utc, "recovery event time")
        ):
            raise RunDatabaseError("recovery event clock is not monotonic")

    def advance_book_head(
        self,
        book_id: str,
        generation: int,
        canonical_book_ref: str,
        *,
        now: datetime,
    ) -> BookHeadV1:
        book_id = _require_nonblank(book_id, "book ID")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("book generation must be a nonnegative integer")
        canonical_book_ref = _require_digest(
            canonical_book_ref, "canonical book reference"
        )
        now_text = _timestamp_text(now, "book-head update time")
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM book_heads WHERE book_id = ?", (book_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO book_heads (
                        book_id, generation, canonical_book_ref, updated_at_utc, version
                    ) VALUES (?, ?, ?, ?, 1)
                    """,
                    (book_id, generation, canonical_book_ref, now_text),
                )
                connection.execute(
                    """
                    INSERT INTO active_snapshots (
                        book_id, snapshot_id, book_generation,
                        pointer_version, updated_at_utc
                    ) VALUES (?, NULL, NULL, 0, ?)
                    """,
                    (book_id, now_text),
                )
            else:
                current = self._book_head_from_row(row)
                self._validate_head_relations(connection, current)
                if now_text < row["updated_at_utc"]:
                    raise ValueError("book-head update time cannot move backward")
                if generation < current.generation or (
                    generation == current.generation
                    and canonical_book_ref != current.canonical_book_ref
                ):
                    raise GenerationRegressionError(
                        "book generation cannot regress or change identity in place"
                    )
                if generation == current.generation:
                    return current
                connection.execute(
                    """
                    UPDATE book_heads
                    SET generation = ?, canonical_book_ref = ?, updated_at_utc = ?,
                        version = version + 1
                    WHERE book_id = ?
                    """,
                    (generation, canonical_book_ref, now_text, book_id),
                )
            updated = connection.execute(
                "SELECT * FROM book_heads WHERE book_id = ?", (book_id,)
            ).fetchone()
            result = self._book_head_from_row(updated)
            self._validate_head_relations(connection, result)
            return result

    def get_book_head(self, book_id: str) -> BookHeadV1 | None:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM book_heads WHERE book_id = ?", (book_id,)
            ).fetchone()
            if row is None:
                return None
            head = self._book_head_from_row(row)
            self._validate_head_relations(connection, head)
            return head

    @staticmethod
    def _idempotency_identity(
        request: NewRunV1,
        captured_generation: int | None,
        client_key_digest: str | None,
    ) -> str:
        return _idempotency_identity_from_preimage(
            run_kind=request.run_kind,
            request_fingerprint=request.request_fingerprint,
            client_idempotency_key_digest=client_key_digest,
            book_id=request.book_id,
            captured_generation=captured_generation,
            target_cut_utc=request.target_cut_utc,
        )

    @staticmethod
    def _validated_run_neighborhood(
        connection: sqlite3.Connection,
        *row_groups: tuple[sqlite3.Row, ...] | list[sqlite3.Row],
    ) -> tuple[RunRecordV1, ...]:
        rows_by_run_id: dict[object, sqlite3.Row] = {}
        for rows in row_groups:
            for row in rows:
                rows_by_run_id.setdefault(row["run_id"], row)
        records = tuple(
            RunRepository._run_from_row(row) for row in rows_by_run_id.values()
        )
        for record in records:
            RunRepository._validate_run_relations(connection, record)
        return records

    def create_or_join(
        self, request: NewRunV1, *, now: datetime
    ) -> CreateRunResultV1:
        if not isinstance(request, NewRunV1):
            raise TypeError("new run must be NewRunV1")
        request = NewRunV1.model_validate(
            request.model_dump(mode="python", warnings=False)
        )
        now_text = _timestamp_text(now, "run request time")
        with self._write_transaction() as connection:
            captured_generation: int | None = None
            expected_active_snapshot_id: str | None = None
            expected_active_pointer_version = 0
            if request.book_id is not None:
                head_row = connection.execute(
                    "SELECT * FROM book_heads WHERE book_id = ?",
                    (request.book_id,),
                ).fetchone()
                if head_row is None:
                    raise RunNotFoundError(
                        f"canonical book head is missing for {request.book_id}"
                    )
                head = self._book_head_from_row(head_row)
                self._validate_head_relations(connection, head)
                captured_generation = head.generation
                if now_text < head_row["updated_at_utc"]:
                    raise ValueError("run request cannot precede its canonical book head")
                pointer_row = self._pointer_row(connection, request.book_id)
                if pointer_row is None:
                    raise RunDatabaseError("book head has no active pointer register")
                pointer = self._pointer_register_from_row(pointer_row)
                if now_text < pointer_row["updated_at_utc"]:
                    raise ValueError(
                        "run request cannot precede its active pointer register"
                    )
                expected_active_snapshot_id = pointer.snapshot_id
                expected_active_pointer_version = pointer.pointer_version

            client_key_digest = (
                None
                if request.client_idempotency_key is None
                else hashlib.sha256(
                    request.client_idempotency_key.encode("utf-8")
                ).hexdigest()
            )
            identity = self._idempotency_identity(
                request, captured_generation, client_key_digest
            )
            target_cut_text = (
                None
                if request.target_cut_utc is None
                else _timestamp_text(request.target_cut_utc, "target cut")
            )
            identity_rows = connection.execute(
                """
                SELECT * FROM snapshot_runs
                WHERE run_kind = ? AND idempotency_identity = ?
                """,
                (request.run_kind, identity),
            ).fetchall()
            preimage_rows = connection.execute(
                """
                SELECT * FROM snapshot_runs
                WHERE run_kind = ?
                  AND request_fingerprint = ?
                  AND client_idempotency_key_digest IS ?
                  AND book_id IS ?
                  AND captured_generation IS ?
                  AND target_cut_utc IS ?
                """,
                (
                    request.run_kind,
                    request.request_fingerprint,
                    client_key_digest,
                    request.book_id,
                    captured_generation,
                    target_cut_text,
                ),
            ).fetchall()
            generation_rows: list[sqlite3.Row] = []
            if request.book_id is not None:
                generation_rows = connection.execute(
                    """
                    SELECT * FROM snapshot_runs
                    WHERE book_id = ? AND captured_generation = ?
                    """,
                    (request.book_id, captured_generation),
                ).fetchall()
            neighborhood_records = self._validated_run_neighborhood(
                connection,
                identity_rows,
                preimage_rows,
                generation_rows,
            )
            identity_run_ids = {row["run_id"] for row in identity_rows}
            generation_run_ids = {row["run_id"] for row in generation_rows}

            compatible = next(
                (
                    record
                    for record in neighborhood_records
                    if record.run_id in identity_run_ids
                    if record.run_outcome is RunOutcome.RUNNING
                ),
                None,
            )
            if compatible is not None:
                return CreateRunResultV1(record=compatible, created=False)

            if any(
                record.run_id in generation_run_ids
                and record.run_outcome is RunOutcome.RUNNING
                for record in neighborhood_records
            ):
                raise IncompatibleLiveRunError(
                    "a different live snapshot run already targets this book generation"
                )

            try:
                connection.execute(
                    """
                    INSERT INTO snapshot_runs (
                        run_id, run_kind, idempotency_identity, request_fingerprint,
                        client_idempotency_key_digest, book_id, captured_generation,
                        expected_active_snapshot_id, expected_active_pointer_version,
                        target_cut_utc, requested_at_utc, started_at_utc, updated_at_utc,
                        finished_at_utc, run_stage, run_outcome,
                        cancel_requested_at_utc, candidate_snapshot_id,
                        published_snapshot_id, result_json, error_code, error_message, version
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL,
                        'QUEUED', 'RUNNING', NULL, NULL, NULL, NULL, NULL, NULL, 1
                    )
                    """,
                    (
                        request.run_id,
                        request.run_kind,
                        identity,
                        request.request_fingerprint,
                        client_key_digest,
                        request.book_id,
                        captured_generation,
                        expected_active_snapshot_id,
                        expected_active_pointer_version,
                        target_cut_text,
                        now_text,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise IncompatibleLiveRunError(
                    "run identity or live book generation conflicts with durable state"
                ) from error
            row = connection.execute(
                "SELECT * FROM snapshot_runs WHERE run_id = ?", (request.run_id,)
            ).fetchone()
            record = self._run_from_row(row)
            self._validate_run_relations(connection, record)
            return CreateRunResultV1(record=record, created=True)

    def get(self, run_id: str) -> RunRecordV1:
        if not isinstance(run_id, str) or not _OPAQUE_ID_RE.fullmatch(run_id):
            raise ValueError("run ID must be a full bounded opaque identifier")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            record = self._run_from_row(row)
            self._validate_run_relations(connection, record)
            return record

    def list_runs(self, *, book_id: str | None = None) -> tuple[RunRecordV1, ...]:
        with self._read_connection() as connection:
            if book_id is None:
                rows = connection.execute(
                    "SELECT * FROM snapshot_runs ORDER BY requested_at_utc, run_id"
                ).fetchall()
            else:
                book_id = _require_nonblank(book_id, "book ID")
                rows = connection.execute(
                    """
                    SELECT * FROM snapshot_runs WHERE book_id = ?
                    ORDER BY requested_at_utc, run_id
                    """,
                    (book_id,),
                ).fetchall()
            records = tuple(self._run_from_row(row) for row in rows)
            completed_tails: set[tuple[str, str]] = set()
            for record in records:
                self._validate_run_relations(
                    connection,
                    record,
                    completed_tails=completed_tails,
                )
            return records

    @staticmethod
    def _running_row(
        connection: sqlite3.Connection,
        run_id: str,
        expected_version: int | None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        record = RunRepository._run_from_row(row)
        RunRepository._validate_run_relations(connection, record)
        if record.run_outcome is not RunOutcome.RUNNING:
            raise TerminalRunMutationError("terminal run records are immutable")
        if expected_version is not None and record.version != expected_version:
            raise StaleRunVersionError("run version does not match durable state")
        return row

    def claim_start(
        self,
        run_id: str,
        *,
        expected_version: int,
        now: datetime,
    ) -> RunRecordV1:
        expected_version = _require_expected_version(expected_version)
        now_text = _timestamp_text(now, "run start time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            _require_monotonic_update(row, now_text)
            if RunStage(row["run_stage"]) is not RunStage.QUEUED:
                raise IllegalRunTransitionError("only QUEUED runs may be claimed")
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_stage = 'INGESTING', started_at_utc = ?, updated_at_utc = ?,
                    version = version + 1
                WHERE run_id = ?
                """,
                (now_text, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def advance_stage(
        self,
        run_id: str,
        stage: RunStage,
        *,
        expected_version: int,
        now: datetime,
    ) -> RunRecordV1:
        if not isinstance(stage, RunStage):
            raise TypeError("stage must be a RunStage")
        expected_version = _require_expected_version(expected_version)
        now_text = _timestamp_text(now, "stage update time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            _require_monotonic_update(row, now_text)
            current = RunStage(row["run_stage"])
            current_index = _STAGE_ORDER.index(current)
            if current_index + 1 >= len(_STAGE_ORDER) or _STAGE_ORDER[
                current_index + 1
            ] is not stage:
                raise IllegalRunTransitionError(
                    "run stages must advance by exactly one adjacent stage"
                )
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_stage = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (stage.value, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def request_cancel(self, run_id: str, *, now: datetime) -> RunRecordV1:
        now_text = _timestamp_text(now, "cancel request time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, None)
            _require_monotonic_update(row, now_text)
            if row["cancel_requested_at_utc"] is not None:
                return self._run_from_row(row)
            connection.execute(
                """
                UPDATE snapshot_runs
                SET cancel_requested_at_utc = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (now_text, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def acknowledge_cancel(
        self,
        run_id: str,
        *,
        expected_version: int,
        now: datetime,
    ) -> RunRecordV1:
        expected_version = _require_expected_version(expected_version)
        now_text = _timestamp_text(now, "cancellation time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            _require_monotonic_update(row, now_text)
            if row["cancel_requested_at_utc"] is None:
                raise IllegalRunTransitionError(
                    "cancellation requires prior durable intent"
                )
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'CANCELLED', error_code = 'CANCELLED_BY_USER',
                    error_message = 'cancelled by user', finished_at_utc = ?, updated_at_utc = ?,
                    version = version + 1
                WHERE run_id = ?
                """,
                (now_text, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def attach_candidate(
        self,
        run_id: str,
        snapshot_id: str,
        *,
        expected_version: int,
        now: datetime,
    ) -> RunRecordV1:
        snapshot_id = _require_digest(snapshot_id, "candidate snapshot ID")
        expected_version = _require_expected_version(expected_version)
        now_text = _timestamp_text(now, "candidate attachment time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            if row["book_id"] is None:
                raise IllegalRunTransitionError(
                    "non-book runs cannot attach snapshot candidates"
                )
            _require_monotonic_update(row, now_text)
            if RunStage(row["run_stage"]) is not RunStage.PUBLISHING:
                raise IllegalRunTransitionError(
                    "candidate identity may be attached only at PUBLISHING"
                )
            if row["candidate_snapshot_id"] is not None:
                if row["candidate_snapshot_id"] == snapshot_id:
                    return self._run_from_row(row)
                raise IllegalRunTransitionError(
                    "candidate snapshot identity is immutable once attached"
                )
            connection.execute(
                """
                UPDATE snapshot_runs
                SET candidate_snapshot_id = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (snapshot_id, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def mark_failed(
        self,
        run_id: str,
        failure: RunFailureV1,
        *,
        expected_version: int | None,
        now: datetime,
    ) -> RunRecordV1:
        if not isinstance(failure, RunFailureV1):
            raise TypeError("failure must be RunFailureV1")
        failure = RunFailureV1.model_validate(
            failure.model_dump(mode="python", warnings=False)
        )
        expected_version = _require_expected_version(expected_version, optional=True)
        now_text = _timestamp_text(now, "failure time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            _require_monotonic_update(row, now_text)
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'FAILED', error_code = ?, error_message = ?,
                    finished_at_utc = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (
                    failure.code.value,
                    _ERROR_MESSAGES[failure.code],
                    now_text,
                    now_text,
                    run_id,
                ),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def complete_nonpublishing(
        self,
        run_id: str,
        result: RunResultV1,
        *,
        expected_version: int | None,
        now: datetime,
    ) -> RunRecordV1:
        if not isinstance(result, RunResultV1):
            raise TypeError("result must be RunResultV1; use adapt_legacy_result explicitly")
        result = RunResultV1.model_validate(
            result.model_dump(mode="python", warnings=False)
        )
        result_json = canonical_json_bytes(result).decode("utf-8")
        _validate_canonical_json_text(result_json, maximum_bytes=_MAX_RESULT_BYTES)
        expected_version = _require_expected_version(expected_version, optional=True)
        now_text = _timestamp_text(now, "completion time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            _require_monotonic_update(row, now_text)
            if row["book_id"] is not None:
                raise IllegalRunTransitionError(
                    "snapshot runs succeed only through atomic publication"
                )
            if row["cancel_requested_at_utc"] is not None:
                raise IllegalRunTransitionError(
                    "a cancelled run cannot accept a successful result"
                )
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'SUCCEEDED', result_json = ?, finished_at_utc = ?,
                    updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (result_json, now_text, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    @staticmethod
    def _pointer_row(
        connection: sqlite3.Connection, book_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT active.*,
                   publication.snapshot_id AS _bound_snapshot_id,
                   publication.book_id AS _bound_book_id,
                   publication.book_generation AS _bound_book_generation,
                   publication.snapshot_status AS _bound_snapshot_status,
                   publication.publication_sequence AS _bound_publication_sequence,
                   publication.published_at_utc AS _bound_published_at_utc
            FROM active_snapshots AS active
            LEFT JOIN snapshot_manifests AS publication
              ON publication.book_id = active.book_id
             AND publication.snapshot_id = active.snapshot_id
             AND publication.book_generation = active.book_generation
            WHERE active.book_id = ?
            """,
            (book_id,),
        ).fetchone()

    @staticmethod
    def _active_row(
        connection: sqlite3.Connection, book_id: str
    ) -> sqlite3.Row | None:
        row = RunRepository._pointer_row(connection, book_id)
        return None if row is None or row["snapshot_id"] is None else row

    @staticmethod
    def _publication_row_for_run(
        connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM snapshot_manifests WHERE run_id = ?", (run_id,)
        ).fetchone()

    @staticmethod
    def _validate_publication_result_neighborhood(
        connection: sqlite3.Connection,
        run: RunRecordV1,
        publication: ManifestPublicationRecordV1 | None,
        pointer: _ActivePointerRegisterV1,
    ) -> None:
        """Validate the fixed indexed neighborhood needed by one result read.

        Full publication ancestry remains an audit/startup concern. This boundary
        proves the target owner tuple, its captured predecessor edge, any stale
        invalidating edge, and the current pointer tail without walking history.
        """

        if run.book_id is None or run.captured_generation is None:
            raise RunDatabaseError("publication result has no durable book identity")
        head_row = connection.execute(
            "SELECT * FROM book_heads WHERE book_id = ?", (run.book_id,)
        ).fetchone()
        if head_row is None:
            raise RunDatabaseError("durable run has no canonical book head")
        head = RunRepository._book_head_from_row(head_row)
        if run.captured_generation > head.generation:
            raise RunDatabaseError("durable run generation exceeds its book head")
        RunRepository._validate_stale_generation_causality(run, head)

        if run.run_outcome is RunOutcome.SUCCEEDED:
            if publication is None:
                raise RunDatabaseError("successful book run has no publication")
            RunRepository._validate_publication_owner_tuple(publication, run)
        elif publication is not None:
            raise RunDatabaseError("non-successful run unexpectedly owns a publication")

        expected_publication: ManifestPublicationRecordV1 | None = None
        if run.expected_active_snapshot_id is not None:
            expected_row = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ? AND snapshot_id = ?
                """,
                (run.book_id, run.expected_active_snapshot_id),
            ).fetchone()
            if expected_row is None:
                raise RunDatabaseError("expected active publication is missing")
            expected_publication = RunRepository._publication_from_row(expected_row)
            owner_row = connection.execute(
                "SELECT * FROM snapshot_runs WHERE run_id = ?",
                (expected_publication.run_id,),
            ).fetchone()
            if owner_row is None:
                raise RunDatabaseError("publication owner run is missing")
            expected_owner = RunRepository._run_from_row(owner_row)
            if (
                expected_owner.book_id != run.book_id
                or expected_owner.captured_generation is None
                or expected_owner.captured_generation > head.generation
            ):
                raise RunDatabaseError("expected publication owner is outside the book")
            RunRepository._validate_publication_owner_tuple(
                expected_publication, expected_owner
            )
            if (
                expected_publication.book_generation > run.captured_generation
                or expected_publication.published_at_utc > run.requested_at_utc
            ):
                raise RunDatabaseError("expected active publication postdates its run")
            if (
                publication is not None
                and expected_publication.publication_sequence
                >= publication.publication_sequence
            ):
                raise RunDatabaseError(
                    "expected-active publication sequence is not historical"
                )

        expected_version = run.expected_active_pointer_version
        if expected_version == 0:
            if run.expected_active_snapshot_id is not None:
                raise RunDatabaseError("virgin pointer capture is malformed")
        else:
            captured_pointer = RunRepository._pointer_transition_at_version(
                connection,
                run.book_id,
                expected_version,
            )
            if (
                captured_pointer.selected_snapshot_id
                != run.expected_active_snapshot_id
                or captured_pointer.transitioned_at_utc > run.requested_at_utc
            ):
                raise RunDatabaseError("run pointer capture is not causal")
            if expected_version == 1:
                if captured_pointer.previous_snapshot_id is not None:
                    raise RunDatabaseError(
                        "first captured pointer transition has a predecessor"
                    )
            else:
                predecessor = RunRepository._pointer_transition_at_version(
                    connection,
                    run.book_id,
                    expected_version - 1,
                )
                if (
                    captured_pointer.previous_snapshot_id
                    != predecessor.selected_snapshot_id
                    or captured_pointer.transitioned_at_utc
                    < predecessor.transitioned_at_utc
                ):
                    raise RunDatabaseError(
                        "captured pointer is not causally linked to its predecessor"
                    )

        if publication is not None:
            publication_transition = RunRepository._pointer_transition_at_version(
                connection,
                run.book_id,
                expected_version + 1,
            )
            if (
                publication_transition.transition_kind != "PUBLICATION"
                or publication_transition.previous_snapshot_id
                != run.expected_active_snapshot_id
                or publication_transition.selected_snapshot_id
                != publication.snapshot_id
                or publication_transition.transitioned_at_utc
                != publication.published_at_utc
            ):
                raise RunDatabaseError(
                    "publication does not match its exact pointer transition"
                )

        if run.error_code is RunErrorCode.STALE_ACTIVE_POINTER:
            invalidating = RunRepository._pointer_transition_at_version(
                connection,
                run.book_id,
                expected_version + 1,
            )
            RunRepository._validate_stale_pointer_causality(run, invalidating)

        RunRepository._validate_pointer_register_relations(connection, pointer)

    def _publication_result_from_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        already_published: bool,
    ) -> PublicationResultV1:
        run_row = connection.execute(
            "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        publication_row = self._publication_row_for_run(connection, run_id)
        pointer_row = None
        if run_row["book_id"] is not None:
            pointer_row = self._pointer_row(connection, run_row["book_id"])
            if pointer_row is None:
                raise RunDatabaseError("book head has no active pointer register")
        run = self._run_from_row(run_row)
        publication_record = (
            None
            if publication_row is None
            else self._publication_from_row(publication_row)
        )
        pointer = (
            None
            if pointer_row is None
            else self._pointer_register_from_row(pointer_row)
        )
        if pointer is not None:
            self._validate_publication_result_neighborhood(
                connection,
                run,
                publication_record,
                pointer,
            )
        active = None if pointer is None else pointer.active_snapshot()
        rejection_code = (
            run.error_code if run.error_code in _PUBLICATION_REJECTION_CODES else None
        )
        if publication_record is None and rejection_code is None:
            raise RunDatabaseError(
                "durable run does not describe a publication result"
            )
        try:
            result = PublicationResultV1(
                run=run,
                publication=publication_record,
                active=active,
                published=publication_row is not None,
                already_published=already_published,
                rejection_code=rejection_code,
            )
            return PublicationResultV1.model_validate(
                result.model_dump(mode="python", warnings=False)
            )
        except (TypeError, ValueError) as error:
            raise RunDatabaseError("durable publication result is invalid") from error

    def resolve_publication_result(
        self, run_id: str, *, already_published: bool
    ) -> PublicationResultV1:
        """Resolve one exact publication attempt from one bounded read transaction."""

        if not isinstance(run_id, str):
            raise TypeError("run ID must be a string")
        if not _OPAQUE_ID_RE.fullmatch(run_id):
            raise ValueError("run ID must be a full bounded opaque identifier")
        if type(already_published) is not bool:
            raise TypeError("already_published must be a boolean")
        with self._read_connection() as connection:
            return RunRepository._publication_result_from_connection(
                self,
                connection,
                run_id,
                already_published=already_published,
            )

    def _publication_result_from_durable_truth(
        self, run_id: str, *, already_published: bool
    ) -> PublicationResultV1:
        result = self.resolve_publication_result(
            run_id, already_published=already_published
        )
        if not result.published or result.run.run_outcome is not RunOutcome.SUCCEEDED:
            raise RunDatabaseError(
                "publication commit outcome could not be resolved from durable truth"
            )
        return result

    @staticmethod
    def _terminalize_publication_rejection(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        code: RunErrorCode,
        now_text: str,
    ) -> None:
        outcome = (
            RunOutcome.CANCELLED
            if code is RunErrorCode.CANCELLED_BY_USER
            else RunOutcome.FAILED
        )
        connection.execute(
            """
            UPDATE snapshot_runs
            SET run_outcome = ?, error_code = ?, error_message = ?,
                finished_at_utc = ?, updated_at_utc = ?, version = version + 1
            WHERE run_id = ? AND run_outcome = 'RUNNING'
            """,
            (
                outcome.value,
                code.value,
                _ERROR_MESSAGES[code],
                now_text,
                now_text,
                run_id,
            ),
        )

    def commit_publication(
        self,
        run_id: str,
        publication: ManifestPublicationV1,
        *,
        expected_version: int,
        now: datetime,
    ) -> PublicationResultV1:
        if not isinstance(publication, ManifestPublicationV1):
            raise TypeError("publication metadata must be ManifestPublicationV1")
        publication = ManifestPublicationV1.model_validate(
            publication.model_dump(mode="python", warnings=False)
        )
        expected_version = _require_expected_version(expected_version)
        now_text = _timestamp_text(now, "publication time")

        connection: sqlite3.Connection | None = None
        committed = False
        try:
            self._inject("db.before_begin")
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            durable_run = self._run_from_row(row)
            self._validate_run_relations(connection, durable_run)

            outcome = RunOutcome(row["run_outcome"])
            if outcome is RunOutcome.SUCCEEDED:
                existing = self._publication_row_for_run(connection, run_id)
                if (
                    existing is not None
                    and existing["snapshot_id"] == publication.snapshot_id
                    and existing["book_id"] == publication.book_id
                    and existing["book_generation"] == publication.book_generation
                    and existing["snapshot_status"] == publication.snapshot_status.value
                    and existing["schema_version"] == publication.schema_version
                    and existing["manifest_relpath"] == publication.manifest_relpath
                    and existing["envelope_sha256"] == publication.envelope_sha256
                    and existing["envelope_byte_length"]
                    == publication.envelope_byte_length
                ):
                    result = self._publication_result_from_connection(
                        connection,
                        run_id,
                        already_published=True,
                    )
                    connection.rollback()
                    return result
                raise PublicationConflictError(
                    "completed run is bound to different publication metadata"
                )
            if outcome is not RunOutcome.RUNNING:
                raise TerminalRunMutationError("terminal run records are immutable")
            if row["cancel_requested_at_utc"] is not None:
                terminal_time_text = max(
                    now_text,
                    row["updated_at_utc"],
                    row["cancel_requested_at_utc"],
                )
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.CANCELLED_BY_USER,
                    now_text=terminal_time_text,
                )
                result = self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                )
                connection.commit()
                committed = True
                return result
            if row["version"] != expected_version:
                raise StaleRunVersionError("run version does not match durable state")
            if RunStage(row["run_stage"]) is not RunStage.PUBLISHING:
                raise IllegalRunTransitionError(
                    "atomic publication requires the PUBLISHING stage"
                )
            if (
                row["book_id"] != publication.book_id
                or row["captured_generation"] != publication.book_generation
                or row["candidate_snapshot_id"] != publication.snapshot_id
            ):
                raise PublicationConflictError(
                    "publication metadata does not match the durable run candidate"
                )

            head = connection.execute(
                "SELECT generation, updated_at_utc FROM book_heads WHERE book_id = ?",
                (row["book_id"],),
            ).fetchone()
            if head is None or head["generation"] != row["captured_generation"]:
                terminal_time_text = max(
                    now_text,
                    row["updated_at_utc"],
                    head["updated_at_utc"] if head is not None else row["updated_at_utc"],
                )
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.STALE_BOOK_GENERATION,
                    now_text=terminal_time_text,
                )
                result = self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                )
                connection.commit()
                committed = True
                return result

            pointer_row = self._pointer_row(connection, publication.book_id)
            if pointer_row is None:
                raise RunDatabaseError("book head has no active pointer register")
            pointer = self._pointer_register_from_row(pointer_row)
            self._validate_pointer_register_relations(connection, pointer)
            expected_active_id = row["expected_active_snapshot_id"]
            expected_pointer_version = row["expected_active_pointer_version"]
            pointer_matches = (
                pointer.snapshot_id == expected_active_id
                and pointer.pointer_version == expected_pointer_version
            )
            if not pointer_matches:
                terminal_time_text = max(
                    now_text,
                    row["updated_at_utc"],
                    pointer_row["updated_at_utc"],
                )
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.STALE_ACTIVE_POINTER,
                    now_text=terminal_time_text,
                )
                result = self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                )
                connection.commit()
                committed = True
                return result

            _require_monotonic_update(row, now_text)
            if now_text < pointer_row["updated_at_utc"]:
                raise ValueError("publication time cannot precede the active pointer update")

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO snapshot_manifests (
                        snapshot_id, run_id, book_id, book_generation, snapshot_status,
                        schema_version, hash_algorithm, manifest_relpath,
                        envelope_sha256, envelope_byte_length, published_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publication.snapshot_id,
                        run_id,
                        publication.book_id,
                        publication.book_generation,
                        publication.snapshot_status.value,
                        publication.schema_version,
                        publication.hash_algorithm,
                        publication.manifest_relpath,
                        publication.envelope_sha256,
                        publication.envelope_byte_length,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise PublicationConflictError(
                    "snapshot identity is already bound to another publication"
                ) from error
            publication_sequence = int(cursor.lastrowid)
            self._inject("db.after_manifest_insert")

            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'SUCCEEDED', published_snapshot_id = ?,
                    finished_at_utc = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ? AND run_outcome = 'RUNNING' AND version = ?
                """,
                (
                    publication.snapshot_id,
                    now_text,
                    now_text,
                    run_id,
                    expected_version,
                ),
            )
            self._inject("db.after_run_update")

            new_pointer_version = expected_pointer_version + 1
            updated = connection.execute(
                """
                UPDATE active_snapshots
                SET snapshot_id = ?, book_generation = ?, pointer_version = ?,
                    updated_at_utc = ?
                WHERE book_id = ? AND snapshot_id IS ? AND book_generation IS ?
                  AND pointer_version = ? AND updated_at_utc = ?
                """,
                (
                    publication.snapshot_id,
                    publication.book_generation,
                    new_pointer_version,
                    now_text,
                    publication.book_id,
                    pointer.snapshot_id,
                    pointer.book_generation,
                    expected_pointer_version,
                    pointer_row["updated_at_utc"],
                ),
            )
            if updated.rowcount != 1:
                raise PublicationConflictError(
                    "active pointer changed during publication"
                )
            self._inject("db.after_active_cas")
            self._publication_result_from_connection(
                connection,
                run_id,
                already_published=False,
            )
            connection.commit()
            committed = True
            try:
                self._inject("db.after_commit")
            except Exception:
                # The transaction is already durable; resolve response uncertainty
                # from the same read path as an ordinary committed result.
                pass
            connection.close()
            connection = None
            result = self._publication_result_from_durable_truth(
                run_id, already_published=False
            )
            if (
                result.publication is None
                or result.publication.publication_sequence != publication_sequence
            ):
                raise RunDatabaseError("publication sequence was not durably bound")
            return result
        except RunRepositoryError:
            if connection is not None and not committed:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None and not committed:
                connection.rollback()
            raise RunDatabaseError("atomic publication transaction failed") from error
        finally:
            if connection is not None:
                connection.close()

    def get_active(self, book_id: str) -> ActiveSnapshotV1 | None:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            row = self._pointer_row(connection, book_id)
            if row is None:
                if connection.execute(
                    "SELECT 1 FROM book_heads WHERE book_id = ?", (book_id,)
                ).fetchone() is not None:
                    raise RunDatabaseError("book head has no active pointer register")
                return None
            register = self._pointer_register_from_row(row)
            active = register.active_snapshot()
            if active is not None:
                self._validate_active_relations(connection, active)
            self._validate_pointer_register_relations(connection, register)
            return active

    def list_active(self) -> tuple[ActiveSnapshotV1, ...]:
        with self._read_connection() as connection:
            if connection.execute(
                """
                SELECT 1
                FROM book_heads AS head
                LEFT JOIN active_snapshots AS pointer
                  ON pointer.book_id = head.book_id
                WHERE pointer.book_id IS NULL
                LIMIT 1
                """
            ).fetchone() is not None:
                raise RunDatabaseError("book head has no active pointer register")
            rows = connection.execute(
                """
                SELECT active.*,
                       publication.snapshot_id AS _bound_snapshot_id,
                       publication.book_id AS _bound_book_id,
                       publication.book_generation AS _bound_book_generation,
                       publication.snapshot_status AS _bound_snapshot_status,
                       publication.publication_sequence AS _bound_publication_sequence,
                       publication.published_at_utc AS _bound_published_at_utc
                FROM active_snapshots AS active
                LEFT JOIN snapshot_manifests AS publication
                  ON publication.book_id = active.book_id
                 AND publication.snapshot_id = active.snapshot_id
                 AND publication.book_generation = active.book_generation
                ORDER BY active.book_id ASC
                """
            ).fetchall()
            registers = tuple(
                self._pointer_register_from_row(row) for row in rows
            )
            active_records = tuple(
                register.active_snapshot() for register in registers
            )
            completed_tails: set[tuple[str, str]] = set()
            for register in registers:
                active = register.active_snapshot()
                if active is not None:
                    self._validate_active_relations(
                        connection,
                        active,
                        completed_tails=completed_tails,
                    )
                self._validate_pointer_register_relations(
                    connection,
                    register,
                    completed_tails=completed_tails,
                )
            return tuple(
                active for active in active_records if active is not None
            )

    def list_publications(
        self, book_id: str
    ) -> tuple[ManifestPublicationRecordV1, ...]:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ?
                ORDER BY publication_sequence DESC
                """,
                (book_id,),
            ).fetchall()
            publications = tuple(self._publication_from_row(row) for row in rows)
            completed_tails: set[tuple[str, str]] = set()
            for publication in publications:
                self._validate_publication_relations(
                    connection,
                    publication,
                    completed_tails=completed_tails,
                )
            return publications

    def list_blessed_fallbacks(
        self, book_id: str, *, excluding: str
    ) -> tuple[ManifestPublicationRecordV1, ...]:
        book_id = _require_nonblank(book_id, "book ID")
        excluding = _require_digest(excluding, "excluded snapshot ID")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ?
                ORDER BY publication_sequence DESC
                """,
                (book_id,),
            ).fetchall()
            publications = tuple(self._publication_from_row(row) for row in rows)
            completed_tails: set[tuple[str, str]] = set()
            for publication in publications:
                self._validate_publication_relations(
                    connection,
                    publication,
                    completed_tails=completed_tails,
                )
            return tuple(
                publication
                for publication in publications
                if publication.snapshot_status is SnapshotStatus.BLESSED
                and publication.snapshot_id != excluding
            )

    def list_recovery_events(self, book_id: str) -> tuple[RecoveryEventV1, ...]:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshot_recovery_events
                WHERE book_id = ? ORDER BY event_sequence
                """,
                (book_id,),
            ).fetchall()
            events = tuple(self._recovery_event_from_row(row) for row in rows)
            completed_tails: set[tuple[str, str]] = set()
            for event in events:
                self._validate_recovery_event_relations(
                    connection,
                    event,
                    completed_tails=completed_tails,
                )
            return events

    @staticmethod
    def _recovery_detail_json(resolution) -> str:
        retained = resolution.failures[:_MAX_RECOVERY_FAILURES]
        detail = {
            "failures": [
                {
                    "error_code": _SELECTOR_REJECTION_CODES.get(
                        failure.error_code,
                        RecoveryRejectionCode.VERIFICATION_FAILED,
                    ).value,
                    "snapshot_id": failure.snapshot_id,
                }
                for failure in retained
            ],
            "omitted_count": len(resolution.failures) - len(retained),
        }
        value = canonical_json_bytes(detail).decode("utf-8")
        _validate_canonical_json_text(
            value, maximum_bytes=_MAX_RECOVERY_JSON_BYTES
        )
        return value

    @staticmethod
    def _closed_verifier(
        verify: SnapshotVerifier,
        expected_publications: dict[str, ManifestPublicationRecordV1],
    ) -> SnapshotVerifier:
        def strict_verify(snapshot_id: str) -> VerifiedSnapshotV1:
            try:
                candidate = verify(snapshot_id)
            except ArtifactNotFoundError as error:
                raise _RecoveryNotFound from error
            except (ManifestIdentityError, ManifestFilenameMismatchError) as error:
                raise _RecoveryIdentityMismatch from error
            except (
                ArtifactDigestMismatchError,
                ArtifactLengthMismatchError,
                NonRegularSnapshotFileError,
                SnapshotVerificationError,
            ) as error:
                raise _RecoveryIntegrityFailure from error
            except ManifestError as error:
                raise _RecoveryInvalidManifest from error
            except Exception as error:
                raise _RecoveryVerificationFailed from error

            try:
                if not isinstance(candidate, VerifiedSnapshotV1):
                    raise TypeError("verifier returned the wrong contract type")
                validated = VerifiedSnapshotV1.model_validate(
                    candidate.model_dump(mode="python", warnings=False)
                )
                verify_manifest(validated.manifest)
                if validated.snapshot_id != snapshot_id:
                    raise ValueError("verifier returned a different snapshot ID")
                if validated.snapshot_id != validated.manifest.snapshot_id:
                    raise ValueError("verified wrapper and manifest IDs disagree")
                if validated.status is not validated.manifest.body.snapshot_status:
                    raise ValueError("verified wrapper and manifest statuses disagree")
                expected = expected_publications.get(snapshot_id)
                if expected is None:
                    raise ValueError("snapshot is absent from the recovery catalog view")
                if validated.manifest.body.book_id != expected.book_id:
                    raise ValueError("manifest book does not match its publication")
                if (
                    validated.manifest.body.book_generation
                    != expected.book_generation
                ):
                    raise ValueError("manifest generation does not match its publication")
                if validated.status is not expected.snapshot_status:
                    raise ValueError("manifest status does not match its publication")
                return validated
            except Exception as error:
                raise _RecoveryInvalidVerifierResult from error

        return strict_verify

    def recover_active(
        self,
        book_id: str,
        *,
        verify: SnapshotVerifier,
        now: datetime,
    ) -> ActiveRecoveryResultV1:
        book_id = _require_nonblank(book_id, "book ID")
        if not callable(verify):
            raise TypeError("snapshot verifier must be callable")
        now_text = _timestamp_text(now, "active recovery time")
        with self._read_connection() as connection:
            completed_tails: set[tuple[str, str]] = set()
            previous_row = self._pointer_row(connection, book_id)
            if previous_row is None:
                if connection.execute(
                    "SELECT 1 FROM book_heads WHERE book_id = ?", (book_id,)
                ).fetchone() is not None:
                    raise RunDatabaseError("book head has no active pointer register")
                previous_pointer = None
                previous_active = None
            else:
                previous_pointer = self._pointer_register_from_row(previous_row)
                self._validate_pointer_register_relations(
                    connection,
                    previous_pointer,
                    completed_tails=completed_tails,
                )
                previous_active = previous_pointer.active_snapshot()
            if previous_active is None:
                active_publication = None
                fallbacks: tuple[ManifestPublicationRecordV1, ...] = ()
            else:
                active_publication = self._validate_active_relations(
                    connection,
                    previous_active,
                    completed_tails=completed_tails,
                )
                fallback_rows = connection.execute(
                    """
                    SELECT * FROM snapshot_manifests
                    WHERE book_id = ?
                    ORDER BY publication_sequence DESC
                    """,
                    (book_id,),
                ).fetchall()
                same_book_publications = tuple(
                    self._publication_from_row(row) for row in fallback_rows
                )
                for publication in same_book_publications:
                    self._validate_publication_relations(
                        connection,
                        publication,
                        completed_tails=completed_tails,
                    )
                fallbacks = tuple(
                    publication
                    for publication in same_book_publications
                    if publication.snapshot_status is SnapshotStatus.BLESSED
                    and publication.snapshot_id != previous_active.snapshot_id
                )
        if previous_active is None:
            return ActiveRecoveryResultV1(
                decision=ActiveRecoveryDecision.UNCHANGED,
                previous_active=None,
                active=None,
                event=None,
            )
        if previous_pointer is None or previous_row is None:
            raise RunDatabaseError("active pointer register disappeared during recovery")
        previous_updated_text = _timestamp_text(
            previous_active.updated_at_utc, "active pointer update time"
        )
        if active_publication is None:
            raise RunDatabaseError("active snapshot has no publication metadata")
        expected_publications = {
            active_publication.snapshot_id: active_publication,
            **{record.snapshot_id: record for record in fallbacks},
        }
        resolution = select_last_good(
            previous_active.snapshot_id,
            tuple(record.snapshot_id for record in fallbacks),
            self._closed_verifier(verify, expected_publications),
        )
        if resolution.resolved_snapshot_id == previous_active.snapshot_id:
            with self._read_connection() as connection:
                current_row = self._pointer_row(connection, book_id)
                if current_row is None:
                    raise RunDatabaseError("book head has no active pointer register")
                current_pointer = self._pointer_register_from_row(current_row)
                self._validate_pointer_register_relations(connection, current_pointer)
                current_active = current_pointer.active_snapshot()
                current_publication = (
                    None
                    if current_active is None
                    else self._validate_active_relations(connection, current_active)
                )
            if current_pointer != previous_pointer:
                return ActiveRecoveryResultV1(
                    decision=ActiveRecoveryDecision.CAS_LOST,
                    previous_active=previous_active,
                    active=current_active,
                    event=None,
                )
            if current_publication != active_publication:
                raise PublicationConflictError(
                    "verified active publication metadata changed before resolution"
                )
            return ActiveRecoveryResultV1(
                decision=ActiveRecoveryDecision.UNCHANGED,
                previous_active=previous_active,
                active=previous_active,
                event=None,
            )

        detail_json = self._recovery_detail_json(resolution)
        self._inject("recovery.after_selection")
        with self._write_transaction() as connection:
            completed_tails: set[tuple[str, str]] = set()
            current_row = self._pointer_row(connection, book_id)
            if current_row is None:
                raise RunDatabaseError("book head has no active pointer register")
            current_pointer = self._pointer_register_from_row(current_row)
            rejected_row = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ? AND snapshot_id = ? AND book_generation = ?
                """,
                (
                    book_id,
                    previous_active.snapshot_id,
                    previous_active.book_generation,
                ),
            ).fetchone()
            rejected_publication = (
                None
                if rejected_row is None
                else self._publication_from_row(rejected_row)
            )
            if (
                rejected_publication is None
                or rejected_publication != active_publication
            ):
                raise PublicationConflictError(
                    "rejected publication metadata changed before resolution"
                )
            pointer_matches = current_pointer == previous_pointer
            selected_id = resolution.resolved_snapshot_id
            selected_publication: ManifestPublicationRecordV1 | None = None
            if selected_id is not None:
                expected_publication = expected_publications[selected_id]
                selected_row = connection.execute(
                    "SELECT * FROM snapshot_manifests WHERE snapshot_id = ?",
                    (selected_id,),
                ).fetchone()
                if selected_row is not None:
                    selected_publication = self._publication_from_row(selected_row)
                if (
                    selected_publication != expected_publication
                    or expected_publication.book_id != book_id
                    or expected_publication.snapshot_status is not SnapshotStatus.BLESSED
                    or selected_id == previous_active.snapshot_id
                ):
                    raise PublicationConflictError(
                        "verified fallback publication metadata changed before resolution"
                    )
            self._validate_pointer_register_relations(
                connection,
                current_pointer,
                completed_tails=completed_tails,
            )
            self._validate_publication_relations(
                connection,
                rejected_publication,
                completed_tails=completed_tails,
            )
            if selected_publication is not None:
                self._validate_publication_relations(
                    connection,
                    selected_publication,
                    completed_tails=completed_tails,
                )
            latest_event_row = connection.execute(
                """
                SELECT recorded_at_utc
                FROM snapshot_recovery_events
                WHERE book_id = ?
                ORDER BY event_sequence DESC
                LIMIT 1
                """,
                (book_id,),
            ).fetchone()
            event_time_text = max(
                now_text,
                current_row["updated_at_utc"],
                _timestamp_text(
                    rejected_publication.published_at_utc,
                    "rejected publication time",
                ),
                (
                    _timestamp_text(
                        selected_publication.published_at_utc,
                        "selected publication time",
                    )
                    if selected_publication is not None
                    else now_text
                ),
                (
                    latest_event_row["recorded_at_utc"]
                    if latest_event_row is not None
                    else now_text
                ),
            )
            if not pointer_matches:
                decision = ActiveRecoveryDecision.CAS_LOST
            else:
                decision = (
                    ActiveRecoveryDecision.REMOVED
                    if selected_id is None
                    else ActiveRecoveryDecision.REPOINTED
                )
                selected_generation = (
                    None
                    if selected_publication is None
                    else selected_publication.book_generation
                )
                updated = connection.execute(
                    """
                    UPDATE active_snapshots
                    SET snapshot_id = ?, book_generation = ?, pointer_version = ?,
                        updated_at_utc = ?
                    WHERE book_id = ? AND snapshot_id IS ? AND book_generation IS ?
                      AND pointer_version = ? AND updated_at_utc = ?
                    """,
                    (
                        selected_id,
                        selected_generation,
                        previous_active.pointer_version + 1,
                        event_time_text,
                        book_id,
                        previous_active.snapshot_id,
                        previous_active.book_generation,
                        previous_active.pointer_version,
                        previous_updated_text,
                    ),
                )
                if updated.rowcount != 1:
                    raise RunDatabaseError("active pointer changed during recovery")

            cursor = connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book_id,
                    previous_active.snapshot_id,
                    previous_active.pointer_version,
                    decision.value,
                    selected_id,
                    detail_json,
                    event_time_text,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM snapshot_recovery_events WHERE event_sequence = ?",
                (cursor.lastrowid,),
            ).fetchone()
            new_pointer_row = self._pointer_row(connection, book_id)
            if new_pointer_row is None:
                raise RunDatabaseError("active pointer register disappeared during recovery")
            new_pointer = self._pointer_register_from_row(new_pointer_row)
            self._validate_pointer_register_relations(
                connection,
                new_pointer,
                completed_tails=completed_tails,
            )
            new_active = new_pointer.active_snapshot()
            event = self._recovery_event_from_row(event_row)
            self._validate_recovery_event_relations(
                connection,
                event,
                completed_tails=completed_tails,
            )
            return ActiveRecoveryResultV1(
                decision=decision,
                previous_active=previous_active,
                active=new_active,
                event=event,
            )

    def recover_interrupted(self, *, now: datetime) -> tuple[str, ...]:
        now_text = _timestamp_text(now, "startup recovery time")
        with self._write_transaction() as connection:
            self._validate_relational_invariants(connection)
            rows = connection.execute(
                """
                SELECT run_id, requested_at_utc, updated_at_utc FROM snapshot_runs
                WHERE run_outcome = 'RUNNING'
                ORDER BY requested_at_utc, run_id
                """
            ).fetchall()
            run_ids = tuple(row["run_id"] for row in rows)
            for row in rows:
                terminal_time_text = max(now_text, row["updated_at_utc"])
                updated = connection.execute(
                    """
                    UPDATE snapshot_runs
                    SET run_outcome = 'FAILED', error_code = 'INTERRUPTED',
                        error_message = 'run interrupted by process restart',
                        finished_at_utc = ?, updated_at_utc = ?, version = version + 1
                    WHERE run_id = ? AND run_outcome = 'RUNNING'
                    """,
                    (terminal_time_text, terminal_time_text, row["run_id"]),
                )
                if updated.rowcount != 1:
                    raise RunDatabaseError(
                        "running row changed during startup recovery"
                    )
            return run_ids


__all__ = [
    "ActiveRecoveryDecision",
    "ActiveRecoveryResultV1",
    "ActiveSnapshotV1",
    "BookHeadV1",
    "ConnectionPragmasV1",
    "CreateRunResultV1",
    "GenerationRegressionError",
    "IllegalRunTransitionError",
    "IncompatibleLiveRunError",
    "ManifestPublicationRecordV1",
    "ManifestPublicationV1",
    "NewRunV1",
    "PublicationConflictError",
    "PublicationResultV1",
    "RecoveryEventV1",
    "RecoveryRejectionCode",
    "RepositoryFaultInjector",
    "RunDatabaseError",
    "RunErrorCode",
    "RunFailureV1",
    "RunNotFoundError",
    "RunRecordV1",
    "RunRepository",
    "RunRepositoryError",
    "RunResultCode",
    "RunResultV1",
    "StaleRunVersionError",
    "TerminalRunMutationError",
    "adapt_legacy_result",
]
