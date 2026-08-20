"""Durable SQLite ownership for snapshot runs, publications, and active pointers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
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
    SnapshotVerifier,
    select_last_good,
)


_CURRENT_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5_000
_MAX_JSON_BYTES = 65_536
_MAX_ERROR_MESSAGE_BYTES = 1_024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|\s)(?:/|[A-Za-z]:[\\/])")
_SECRET_MARKER_RE = re.compile(
    r"(?:authorization\s*:|password\s*=|api[_-]?key\s*=|token\s*=)", re.IGNORECASE
)
_STAGE_ORDER = (
    RunStage.QUEUED,
    RunStage.INGESTING,
    RunStage.RECONCILING,
    RunStage.VALIDATING,
    RunStage.MODELING,
    RunStage.PUBLISHING,
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


class ActiveRecoveryDecision(str, Enum):
    UNCHANGED = "UNCHANGED"
    REPOINTED = "REPOINTED"
    REMOVED = "REMOVED"
    CAS_LOST = "CAS_LOST"


def _require_nonblank(value: str, field_name: str, *, limit: int = 256) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} exceeds its bounded length")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} contains control characters")
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
    return _require_utc(value, field_name).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _safe_message(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("error message must be a string")
    _require_nonblank(value, "error message", limit=_MAX_ERROR_MESSAGE_BYTES)
    if _ABSOLUTE_PATH_RE.search(value):
        raise ValueError("error message must not contain an absolute path")
    if _SECRET_MARKER_RE.search(value):
        raise ValueError("error message must not contain credential-like material")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _validate_canonical_json_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("result JSON must be a string")
    payload = value.encode("utf-8")
    if len(payload) > _MAX_JSON_BYTES:
        raise ValueError("JSON exceeds the durable ledger size limit")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("result JSON is invalid") from error
    if canonical_json_bytes(parsed) != payload:
        raise ValueError("result JSON must use canonical JSON encoding")
    return value


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
    message: str

    @field_validator("message")
    @classmethod
    def _message_is_safe(cls, value: str) -> str:
        return _safe_message(value)


class BookHeadV1(FrozenContractBase):
    book_id: str
    generation: int = Field(ge=0)
    canonical_book_ref: str
    updated_at_utc: datetime
    version: int = Field(ge=1)


class RunRecordV1(FrozenContractBase):
    run_id: str
    run_kind: str
    idempotency_identity: str
    request_fingerprint: str
    client_idempotency_key: str | None
    book_id: str | None
    captured_generation: int | None
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
    result_json: str | None
    error_code: RunErrorCode | None
    error_message: str | None
    version: int = Field(ge=1)


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


class ActiveSnapshotV1(FrozenContractBase):
    book_id: str
    snapshot_id: str
    book_generation: int = Field(ge=0)
    pointer_version: int = Field(ge=1)
    updated_at_utc: datetime


class PublicationResultV1(FrozenContractBase):
    run: RunRecordV1
    publication: ManifestPublicationRecordV1 | None
    active: ActiveSnapshotV1 | None
    published: bool
    already_published: bool
    rejection_code: RunErrorCode | None


class RecoveryEventV1(FrozenContractBase):
    event_sequence: int = Field(ge=1)
    book_id: str
    rejected_snapshot_id: str
    expected_pointer_version: int = Field(ge=1)
    resolution_action: ActiveRecoveryDecision
    selected_snapshot_id: str | None
    detail_json: str
    recorded_at_utc: datetime


class ActiveRecoveryResultV1(FrozenContractBase):
    decision: ActiveRecoveryDecision
    previous_active: ActiveSnapshotV1 | None
    active: ActiveSnapshotV1 | None
    event: RecoveryEventV1 | None


RepositoryFaultInjector = Callable[[str], None]


class RunRepository:
    """Short-connection SQLite repository; workers never receive this object."""

    def __init__(
        self,
        root: Path,
        *,
        fault_injector: RepositoryFaultInjector | None = None,
    ) -> None:
        self.root = Path(root)
        self.database_path = self.root / "snapshots" / "runs.sqlite3"
        self._fault_injector = fault_injector

    def _inject(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=_BUSY_TIMEOUT_MS / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise RunDatabaseError("cannot open the durable run database") from error

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
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

    def initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RunDatabaseError("cannot create the durable run database root") from error
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                migration = (
                    resources.files("quantmind.snapshots.migrations")
                    .joinpath("0001_run_catalog.sql")
                    .read_text(encoding="utf-8")
                )
                connection.executescript(f"BEGIN IMMEDIATE;\n{migration}\nCOMMIT;")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _CURRENT_SCHEMA_VERSION:
                raise RunDatabaseError(
                    f"unsupported durable run schema version: {version}"
                )
        except RunRepositoryError:
            raise
        except (OSError, sqlite3.Error) as error:
            connection.rollback()
            raise RunDatabaseError("cannot initialize the durable run database") from error
        finally:
            connection.close()

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
        return BookHeadV1(
            book_id=row["book_id"],
            generation=row["generation"],
            canonical_book_ref=row["canonical_book_ref"],
            updated_at_utc=_parse_timestamp(row["updated_at_utc"]),
            version=row["version"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecordV1:
        return RunRecordV1(
            run_id=row["run_id"],
            run_kind=row["run_kind"],
            idempotency_identity=row["idempotency_identity"],
            request_fingerprint=row["request_fingerprint"],
            client_idempotency_key=row["client_idempotency_key"],
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
            result_json=row["result_json"],
            error_code=(
                None if row["error_code"] is None else RunErrorCode(row["error_code"])
            ),
            error_message=row["error_message"],
            version=row["version"],
        )

    @staticmethod
    def _publication_from_row(row: sqlite3.Row) -> ManifestPublicationRecordV1:
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

    @staticmethod
    def _active_from_row(row: sqlite3.Row) -> ActiveSnapshotV1:
        return ActiveSnapshotV1(
            book_id=row["book_id"],
            snapshot_id=row["snapshot_id"],
            book_generation=row["book_generation"],
            pointer_version=row["pointer_version"],
            updated_at_utc=_parse_timestamp(row["updated_at_utc"]),
        )

    @staticmethod
    def _recovery_event_from_row(row: sqlite3.Row) -> RecoveryEventV1:
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
            else:
                current = self._book_head_from_row(row)
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
            return self._book_head_from_row(updated)

    def get_book_head(self, book_id: str) -> BookHeadV1 | None:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM book_heads WHERE book_id = ?", (book_id,)
            ).fetchone()
            return None if row is None else self._book_head_from_row(row)

    @staticmethod
    def _idempotency_identity(
        request: NewRunV1, captured_generation: int | None
    ) -> str:
        payload = {
            "book_id": request.book_id,
            "captured_generation": captured_generation,
            "client_idempotency_key": request.client_idempotency_key,
            "request_fingerprint": request.request_fingerprint,
            "run_kind": request.run_kind,
            "target_cut_utc": request.target_cut_utc,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

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
                head = connection.execute(
                    "SELECT generation FROM book_heads WHERE book_id = ?",
                    (request.book_id,),
                ).fetchone()
                if head is None:
                    raise RunNotFoundError(
                        f"canonical book head is missing for {request.book_id}"
                    )
                captured_generation = int(head["generation"])
                active = connection.execute(
                    "SELECT snapshot_id, pointer_version FROM active_snapshots WHERE book_id = ?",
                    (request.book_id,),
                ).fetchone()
                if active is not None:
                    expected_active_snapshot_id = active["snapshot_id"]
                    expected_active_pointer_version = int(active["pointer_version"])

            identity = self._idempotency_identity(request, captured_generation)
            compatible = connection.execute(
                """
                SELECT * FROM snapshot_runs
                WHERE run_kind = ? AND idempotency_identity = ?
                  AND run_outcome = 'RUNNING'
                """,
                (request.run_kind, identity),
            ).fetchone()
            if compatible is not None:
                return CreateRunResultV1(
                    record=self._run_from_row(compatible), created=False
                )

            if request.book_id is not None:
                incompatible = connection.execute(
                    """
                    SELECT run_id FROM snapshot_runs
                    WHERE book_id = ? AND captured_generation = ?
                      AND run_outcome = 'RUNNING'
                    """,
                    (request.book_id, captured_generation),
                ).fetchone()
                if incompatible is not None:
                    raise IncompatibleLiveRunError(
                        "a different live snapshot run already targets this book generation"
                    )

            try:
                connection.execute(
                    """
                    INSERT INTO snapshot_runs (
                        run_id, run_kind, idempotency_identity, request_fingerprint,
                        client_idempotency_key, book_id, captured_generation,
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
                        request.client_idempotency_key,
                        request.book_id,
                        captured_generation,
                        expected_active_snapshot_id,
                        expected_active_pointer_version,
                        (
                            None
                            if request.target_cut_utc is None
                            else _timestamp_text(request.target_cut_utc, "target cut")
                        ),
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
            return CreateRunResultV1(record=self._run_from_row(row), created=True)

    def get(self, run_id: str) -> RunRecordV1:
        if not isinstance(run_id, str) or not _OPAQUE_ID_RE.fullmatch(run_id):
            raise ValueError("run ID must be a full bounded opaque identifier")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            return self._run_from_row(row)

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
            return tuple(self._run_from_row(row) for row in rows)

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
        if RunOutcome(row["run_outcome"]) is not RunOutcome.RUNNING:
            raise TerminalRunMutationError("terminal run records are immutable")
        if expected_version is not None and row["version"] != expected_version:
            raise StaleRunVersionError("run version does not match durable state")
        return row

    def claim_start(
        self,
        run_id: str,
        *,
        expected_version: int,
        now: datetime,
    ) -> RunRecordV1:
        now_text = _timestamp_text(now, "run start time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
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
        now_text = _timestamp_text(now, "stage update time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
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
        now_text = _timestamp_text(now, "cancellation time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
            if row["cancel_requested_at_utc"] is None:
                raise IllegalRunTransitionError(
                    "cancellation requires prior durable intent"
                )
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'CANCELLED', error_code = 'CANCELLED_BY_USER',
                    error_message = NULL, finished_at_utc = ?, updated_at_utc = ?,
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
        now_text = _timestamp_text(now, "candidate attachment time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
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
        now_text = _timestamp_text(now, "failure time")
        with self._write_transaction() as connection:
            self._running_row(connection, run_id, expected_version)
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'FAILED', error_code = ?, error_message = ?,
                    finished_at_utc = ?, updated_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (failure.code.value, failure.message, now_text, now_text, run_id),
            )
            return self._run_from_row(
                connection.execute(
                    "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def complete_nonpublishing(
        self,
        run_id: str,
        result_json: str,
        *,
        expected_version: int | None,
        now: datetime,
    ) -> RunRecordV1:
        result_json = _validate_canonical_json_text(result_json)
        now_text = _timestamp_text(now, "completion time")
        with self._write_transaction() as connection:
            row = self._running_row(connection, run_id, expected_version)
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
    def _active_row(
        connection: sqlite3.Connection, book_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM active_snapshots WHERE book_id = ?", (book_id,)
        ).fetchone()

    @staticmethod
    def _publication_row_for_run(
        connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM snapshot_manifests WHERE run_id = ?", (run_id,)
        ).fetchone()

    def _publication_result_from_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        already_published: bool,
        rejection_code: RunErrorCode | None = None,
    ) -> PublicationResultV1:
        run_row = connection.execute(
            "SELECT * FROM snapshot_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        publication_row = self._publication_row_for_run(connection, run_id)
        active_row = (
            None
            if run_row["book_id"] is None
            else self._active_row(connection, run_row["book_id"])
        )
        return PublicationResultV1(
            run=self._run_from_row(run_row),
            publication=(
                None
                if publication_row is None
                else self._publication_from_row(publication_row)
            ),
            active=None if active_row is None else self._active_from_row(active_row),
            published=publication_row is not None,
            already_published=already_published,
            rejection_code=rejection_code,
        )

    def _publication_result_from_durable_truth(
        self, run_id: str, *, already_published: bool
    ) -> PublicationResultV1:
        with self._read_connection() as connection:
            result = self._publication_result_from_connection(
                connection,
                run_id,
                already_published=already_published,
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
        message: str,
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
                None if outcome is RunOutcome.CANCELLED else _safe_message(message),
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
                    connection.rollback()
                    return self._publication_result_from_connection(
                        connection,
                        run_id,
                        already_published=True,
                    )
                raise PublicationConflictError(
                    "completed run is bound to different publication metadata"
                )
            if outcome is not RunOutcome.RUNNING:
                raise TerminalRunMutationError("terminal run records are immutable")
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

            if row["cancel_requested_at_utc"] is not None:
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.CANCELLED_BY_USER,
                    message="cancelled",
                    now_text=now_text,
                )
                connection.commit()
                committed = True
                return self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                    rejection_code=RunErrorCode.CANCELLED_BY_USER,
                )

            head = connection.execute(
                "SELECT generation FROM book_heads WHERE book_id = ?",
                (row["book_id"],),
            ).fetchone()
            if head is None or head["generation"] != row["captured_generation"]:
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.STALE_BOOK_GENERATION,
                    message="canonical book generation changed before publication",
                    now_text=now_text,
                )
                connection.commit()
                committed = True
                return self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                    rejection_code=RunErrorCode.STALE_BOOK_GENERATION,
                )

            active_row = self._active_row(connection, publication.book_id)
            expected_active_id = row["expected_active_snapshot_id"]
            expected_pointer_version = row["expected_active_pointer_version"]
            pointer_matches = (
                active_row is None
                and expected_active_id is None
                and expected_pointer_version == 0
            ) or (
                active_row is not None
                and active_row["snapshot_id"] == expected_active_id
                and active_row["pointer_version"] == expected_pointer_version
            )
            if not pointer_matches:
                self._terminalize_publication_rejection(
                    connection,
                    run_id=run_id,
                    code=RunErrorCode.STALE_ACTIVE_POINTER,
                    message="active snapshot pointer changed before publication",
                    now_text=now_text,
                )
                connection.commit()
                committed = True
                return self._publication_result_from_connection(
                    connection,
                    run_id,
                    already_published=False,
                    rejection_code=RunErrorCode.STALE_ACTIVE_POINTER,
                )

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
            if active_row is None:
                connection.execute(
                    """
                    INSERT INTO active_snapshots (
                        book_id, snapshot_id, book_generation, pointer_version, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        publication.book_id,
                        publication.snapshot_id,
                        publication.book_generation,
                        new_pointer_version,
                        now_text,
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE active_snapshots
                    SET snapshot_id = ?, book_generation = ?, pointer_version = ?,
                        updated_at_utc = ?
                    WHERE book_id = ? AND snapshot_id = ? AND pointer_version = ?
                    """,
                    (
                        publication.snapshot_id,
                        publication.book_generation,
                        new_pointer_version,
                        now_text,
                        publication.book_id,
                        expected_active_id,
                        expected_pointer_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise PublicationConflictError(
                        "active pointer changed during publication"
                    )
            self._inject("db.after_active_cas")
            connection.commit()
            committed = True
            try:
                self._inject("db.after_commit")
            except BaseException:
                connection.close()
                connection = None
                return self._publication_result_from_durable_truth(
                    run_id, already_published=False
                )
            result = self._publication_result_from_connection(
                connection,
                run_id,
                already_published=False,
            )
            if (
                result.publication is None
                or result.publication.publication_sequence != publication_sequence
            ):
                raise RunDatabaseError("publication sequence was not durably bound")
            return result
        except (
            RunNotFoundError,
            IllegalRunTransitionError,
            StaleRunVersionError,
            TerminalRunMutationError,
            PublicationConflictError,
        ):
            if connection is not None and not committed:
                connection.rollback()
            raise
        except BaseException as error:
            if connection is not None and not committed:
                connection.rollback()
            raise RunDatabaseError("atomic publication transaction failed") from error
        finally:
            if connection is not None:
                connection.close()

    def get_active(self, book_id: str) -> ActiveSnapshotV1 | None:
        book_id = _require_nonblank(book_id, "book ID")
        with self._read_connection() as connection:
            row = self._active_row(connection, book_id)
            return None if row is None else self._active_from_row(row)

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
            return tuple(self._publication_from_row(row) for row in rows)

    def list_blessed_fallbacks(
        self, book_id: str, *, excluding: str
    ) -> tuple[ManifestPublicationRecordV1, ...]:
        book_id = _require_nonblank(book_id, "book ID")
        excluding = _require_digest(excluding, "excluded snapshot ID")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshot_manifests
                WHERE book_id = ? AND snapshot_status = 'BLESSED' AND snapshot_id <> ?
                ORDER BY publication_sequence DESC
                """,
                (book_id, excluding),
            ).fetchall()
            return tuple(self._publication_from_row(row) for row in rows)

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
            return tuple(self._recovery_event_from_row(row) for row in rows)

    @staticmethod
    def _recovery_detail_json(resolution) -> str:
        # Filesystem errors may contain paths or payload fragments. The durable catalog keeps
        # only typed codes and full immutable IDs; operational detail stays in scoped logs.
        detail = {
            "failures": [
                {
                    "error_code": failure.error_code,
                    "snapshot_id": failure.snapshot_id,
                }
                for failure in resolution.failures
            ]
        }
        value = canonical_json_bytes(detail).decode("utf-8")
        return _validate_canonical_json_text(value)

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
        previous_active = self.get_active(book_id)
        if previous_active is None:
            return ActiveRecoveryResultV1(
                decision=ActiveRecoveryDecision.UNCHANGED,
                previous_active=None,
                active=None,
                event=None,
            )
        fallbacks = self.list_blessed_fallbacks(
            book_id, excluding=previous_active.snapshot_id
        )
        resolution = select_last_good(
            previous_active.snapshot_id,
            tuple(record.snapshot_id for record in fallbacks),
            verify,
        )
        if resolution.resolved_snapshot_id == previous_active.snapshot_id:
            return ActiveRecoveryResultV1(
                decision=ActiveRecoveryDecision.UNCHANGED,
                previous_active=previous_active,
                active=previous_active,
                event=None,
            )

        detail_json = self._recovery_detail_json(resolution)
        self._inject("recovery.after_selection")
        with self._write_transaction() as connection:
            current_row = self._active_row(connection, book_id)
            pointer_matches = (
                current_row is not None
                and current_row["snapshot_id"] == previous_active.snapshot_id
                and current_row["pointer_version"] == previous_active.pointer_version
            )
            selected_id = resolution.resolved_snapshot_id
            if not pointer_matches:
                decision = ActiveRecoveryDecision.CAS_LOST
            elif selected_id is None:
                connection.execute(
                    """
                    DELETE FROM active_snapshots
                    WHERE book_id = ? AND snapshot_id = ? AND pointer_version = ?
                    """,
                    (
                        book_id,
                        previous_active.snapshot_id,
                        previous_active.pointer_version,
                    ),
                )
                decision = ActiveRecoveryDecision.REMOVED
            else:
                selected_publication = connection.execute(
                    """
                    SELECT * FROM snapshot_manifests
                    WHERE book_id = ? AND snapshot_id = ? AND snapshot_status = 'BLESSED'
                    """,
                    (book_id, selected_id),
                ).fetchone()
                if selected_publication is None:
                    raise PublicationConflictError(
                        "verified fallback is absent from BLESSED publication history"
                    )
                connection.execute(
                    """
                    UPDATE active_snapshots
                    SET snapshot_id = ?, book_generation = ?, pointer_version = ?,
                        updated_at_utc = ?
                    WHERE book_id = ? AND snapshot_id = ? AND pointer_version = ?
                    """,
                    (
                        selected_id,
                        selected_publication["book_generation"],
                        previous_active.pointer_version + 1,
                        now_text,
                        book_id,
                        previous_active.snapshot_id,
                        previous_active.pointer_version,
                    ),
                )
                decision = ActiveRecoveryDecision.REPOINTED

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
                    now_text,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM snapshot_recovery_events WHERE event_sequence = ?",
                (cursor.lastrowid,),
            ).fetchone()
            new_active_row = self._active_row(connection, book_id)
            return ActiveRecoveryResultV1(
                decision=decision,
                previous_active=previous_active,
                active=(
                    None
                    if new_active_row is None
                    else self._active_from_row(new_active_row)
                ),
                event=self._recovery_event_from_row(event_row),
            )

    def recover_interrupted(self, *, now: datetime) -> tuple[str, ...]:
        now_text = _timestamp_text(now, "startup recovery time")
        with self._write_transaction() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM snapshot_runs
                WHERE run_outcome = 'RUNNING'
                ORDER BY requested_at_utc, run_id
                """
            ).fetchall()
            run_ids = tuple(row["run_id"] for row in rows)
            if run_ids:
                connection.execute(
                    """
                    UPDATE snapshot_runs
                    SET run_outcome = 'FAILED', error_code = 'INTERRUPTED',
                        error_message = 'run interrupted by process restart',
                        finished_at_utc = ?, updated_at_utc = ?, version = version + 1
                    WHERE run_outcome = 'RUNNING'
                    """,
                    (now_text, now_text),
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
    "RepositoryFaultInjector",
    "RunDatabaseError",
    "RunErrorCode",
    "RunFailureV1",
    "RunNotFoundError",
    "RunRecordV1",
    "RunRepository",
    "RunRepositoryError",
    "StaleRunVersionError",
    "TerminalRunMutationError",
]
