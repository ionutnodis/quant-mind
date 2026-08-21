from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantmind.snapshots.contracts import (
    GateEvidenceV1,
    GateStatus,
    RecoveryClass,
    RunOutcome,
    RunStage,
    SnapshotStatus,
    ValuationCutV1,
)
from quantmind.snapshots.input_artifacts import ArtifactRefV1
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestBodyV1,
    ManifestPolicyEvidenceV1,
    OutputArtifactBindingV1,
    create_manifest,
)
from quantmind.snapshots.run_repository import (
    ActiveRecoveryDecision,
    GenerationRegressionError,
    IllegalRunTransitionError,
    IncompatibleLiveRunError,
    ManifestPublicationV1,
    NewRunV1,
    PublicationConflictError,
    PublicationResultV1,
    RecoveryEventV1,
    RecoveryRejectionCode,
    RunDatabaseError,
    RunErrorCode,
    RunFailureV1,
    RunNotFoundError,
    RunRepository,
    RunResultCode,
    RunResultV1,
    StaleRunVersionError,
    TerminalRunMutationError,
    adapt_legacy_result,
)
from quantmind.snapshots.store import VerifiedSnapshotV1


T0 = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 20, 8, 1, tzinfo=UTC)
T2 = datetime(2026, 8, 20, 8, 2, tzinfo=UTC)
T3 = datetime(2026, 8, 20, 8, 3, tzinfo=UTC)
T4 = datetime(2026, 8, 20, 8, 4, tzinfo=UTC)
T5 = datetime(2026, 8, 20, 8, 5, tzinfo=UTC)
T6 = datetime(2026, 8, 20, 8, 6, tzinfo=UTC)
T7 = datetime(2026, 8, 20, 8, 7, tzinfo=UTC)
T8 = datetime(2026, 8, 20, 8, 8, tzinfo=UTC)
BOOK_REF_1 = "1" * 64
BOOK_REF_2 = "2" * 64


def _verified_fixture(
    label: str,
    *,
    book_id: str = "book-alpha",
    book_generation: int = 1,
) -> VerifiedSnapshotV1:
    book_ref = ArtifactRefV1(
        hash_algorithm="sha256",
        digest="f" * 64,
        byte_length=1,
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    output_ref = ArtifactRefV1(
        hash_algorithm="sha256",
        digest=label * 64,
        byte_length=1,
        media_type="application/json",
        schema_version="xray_v1",
    )
    body = AnalyticalSnapshotManifestBodyV1(
        schema_version="analytical_snapshot_manifest_v1",
        canonicalization_version="quantmind_canonical_json_v1",
        hash_algorithm="sha256",
        book_id=book_id,
        book_generation=book_generation,
        legacy_book_ref=None,
        valuation_cut=ValuationCutV1(
            target_cut_utc=T0,
            display_timezone="UTC",
            capture_start_utc=T0,
            capture_end_utc=T1,
        ),
        base_currency="USD",
        normalized_nlv=Decimal("1000.00"),
        included_account_ids=("account-a",),
        canonical_book_ref=book_ref,
        canonical_book_hash=book_ref.digest,
        position_hash="9" * 64,
        input_artifacts=(),
        security_master_mapping_version="security-v1",
        corporate_action_version=None,
        calendar_version=None,
        rights_manifest_versions=(),
        factor_taxonomy_version="factor-v1",
        return_series_version="returns-v1",
        production_covariance_model_version="covariance-v1",
        residual_model_version="residual-v1",
        latent_factor_model_version=None,
        option_pricer_version=None,
        surface_model_version=None,
        scenario_library_version=None,
        analytical_config_hash="8" * 64,
        application_commit="2a7f70a",
        application_build_id=f"fixture-{label}",
        snapshot_status=SnapshotStatus.BLESSED,
        gates=(
            GateEvidenceV1(
                gate_code="OUTPUT_GATE",
                status=GateStatus.PASSED,
                recovery_class=RecoveryClass.MODEL_OWNER_UPDATE,
                evidence=("synthetic verified fixture",),
                recovery_action="none",
            ),
        ),
        policy_evidence=(
            ManifestPolicyEvidenceV1(
                subject_kind="OUTPUT",
                subject_id=f"xray-{label}",
                gate_code="OUTPUT_GATE",
            ),
        ),
        warnings=(),
        refused_outputs=(),
        outputs=(
            OutputArtifactBindingV1(
                logical_role="XRAY_READ_MODEL",
                logical_id=f"xray-{label}",
                object_ref=output_ref,
                model_version="xray-v1",
            ),
        ),
    )
    manifest = create_manifest(body)
    return VerifiedSnapshotV1(
        snapshot_id=manifest.snapshot_id,
        status=manifest.body.snapshot_status,
        manifest=manifest,
    )


VERIFIED_A = _verified_fixture("a")
VERIFIED_B = _verified_fixture("b")
VERIFIED_C = _verified_fixture("c")
VERIFIED_BETA = _verified_fixture("d", book_id="book-beta")
VERIFIED_ALPHA_G2 = _verified_fixture("e", book_generation=2)
SNAPSHOT_A = VERIFIED_A.snapshot_id
SNAPSHOT_B = VERIFIED_B.snapshot_id
SNAPSHOT_C = VERIFIED_C.snapshot_id
SNAPSHOT_BETA = VERIFIED_BETA.snapshot_id
SNAPSHOT_ALPHA_G2 = VERIFIED_ALPHA_G2.snapshot_id


def _repository(tmp_path: Path) -> RunRepository:
    repository = RunRepository(tmp_path)
    repository.initialize()
    return repository


def _raw_journal_mode(repository: RunRepository) -> str:
    database_uri = f"{repository.database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _new_run(
    run_id: str = "run_01J5X5S8J5J8P7KQ4Y0T3N6M9A",
    *,
    request_fingerprint: str = "a" * 64,
    client_idempotency_key: str | None = "refresh-button",
    book_id: str | None = "book-alpha",
) -> NewRunV1:
    return NewRunV1(
        run_id=run_id,
        run_kind="ANALYTICAL_SNAPSHOT" if book_id else "SYNC",
        request_fingerprint=request_fingerprint,
        client_idempotency_key=client_idempotency_key,
        book_id=book_id,
        target_cut_utc=T0 if book_id else None,
    )


def _allocated(repository: RunRepository, *, run_id: str = "run_01J5X5S8J5J8P7KQ4Y0T3N6M9A"):
    repository.advance_book_head(
        book_id="book-alpha",
        generation=1,
        canonical_book_ref=BOOK_REF_1,
        now=T0,
    )
    return repository.create_or_join(_new_run(run_id), now=T1).record


def _publishing_run(
    repository: RunRepository,
    *,
    run_id: str = "run_01J5X5S8J5J8P7KQ4Y0T3N6M9A",
    snapshot_id: str = SNAPSHOT_A,
    book_id: str = "book-alpha",
):
    active = repository.get_active(book_id)
    request_time = (
        T1
        if active is None
        else max(T1, active.updated_at_utc)
    )
    work_time = max(T2, request_time)
    record = repository.create_or_join(
        _new_run(run_id, book_id=book_id), now=request_time
    ).record
    record = repository.claim_start(
        record.run_id, expected_version=record.version, now=request_time
    )
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        record = repository.advance_stage(
            record.run_id, stage, expected_version=record.version, now=work_time
        )
    return repository.attach_candidate(
        record.run_id,
        snapshot_id,
        expected_version=record.version,
        now=work_time,
    )


def _publishing_run_at(
    repository: RunRepository,
    *,
    run_id: str,
    snapshot_id: str,
    now: datetime,
    record=None,
):
    if record is None:
        record = repository.create_or_join(_new_run(run_id), now=now).record
    record = repository.claim_start(
        record.run_id, expected_version=record.version, now=now
    )
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        record = repository.advance_stage(
            record.run_id, stage, expected_version=record.version, now=now
        )
    return repository.attach_candidate(
        record.run_id,
        snapshot_id,
        expected_version=record.version,
        now=now,
    )


def _failed_nonbook_run(
    repository: RunRepository,
    *,
    run_id: str,
    client_idempotency_key: str,
):
    record = repository.create_or_join(
        _new_run(
            run_id=run_id,
            book_id=None,
            client_idempotency_key=client_idempotency_key,
        ),
        now=T0,
    ).record
    return repository.mark_failed(
        record.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=record.version,
        now=T1,
    )


def _publication(
    snapshot_id: str,
    *,
    generation: int,
    status: SnapshotStatus = SnapshotStatus.BLESSED,
    book_id: str = "book-alpha",
) -> ManifestPublicationV1:
    return ManifestPublicationV1(
        snapshot_id=snapshot_id,
        book_id=book_id,
        book_generation=generation,
        snapshot_status=status,
        schema_version="analytical_snapshot_manifest_v1",
        hash_algorithm="sha256",
        manifest_relpath=(
            "snapshots/manifests/analytical_snapshot_manifest_v1/"
            f"{snapshot_id[:2]}/{snapshot_id}.json"
        ),
        envelope_sha256="e" * 64,
        envelope_byte_length=4_096,
    )


def _publish_alpha_and_beta(repository: RunRepository) -> None:
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    repository.advance_book_head("book-beta", 1, BOOK_REF_1, now=T0)
    alpha = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        alpha.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=alpha.version,
        now=T3,
    )
    beta = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6B1T",
        snapshot_id=SNAPSHOT_BETA,
        book_id="book-beta",
    )
    repository.commit_publication(
        beta.run_id,
        _publication(SNAPSHOT_BETA, generation=1, book_id="book-beta"),
        expected_version=beta.version,
        now=T3,
    )


def _publish_second_alpha(
    repository: RunRepository,
    *,
    run_id: str = "run_01J5X5S8J5J8P7KQ4Y0T3N6F2X",
) -> None:
    second = _publishing_run(
        repository,
        run_id=run_id,
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )


def _verified(snapshot_id: str) -> VerifiedSnapshotV1:
    return {
        SNAPSHOT_A: VERIFIED_A,
        SNAPSHOT_B: VERIFIED_B,
        SNAPSHOT_C: VERIFIED_C,
        SNAPSHOT_BETA: VERIFIED_BETA,
        SNAPSHOT_ALPHA_G2: VERIFIED_ALPHA_G2,
    }[snapshot_id]


def _seed_nonbook_history(repository: RunRepository, count: int) -> None:
    timestamp = "2026-08-20T08:00:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            WITH RECURSIVE sequence(value) AS (
                VALUES (0)
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value + 1 < ?
            )
            INSERT INTO snapshot_runs (
                run_id, run_kind, idempotency_identity, request_fingerprint,
                client_idempotency_key_digest, book_id, captured_generation,
                expected_active_snapshot_id, expected_active_pointer_version,
                target_cut_utc, requested_at_utc, started_at_utc, updated_at_utc,
                finished_at_utc, run_stage, run_outcome,
                cancel_requested_at_utc, candidate_snapshot_id,
                published_snapshot_id, result_json, error_code, error_message, version
            )
            SELECT
                printf('run_perf_%020d', value), 'SYNC', printf('%064x', value + 1),
                printf('%064x', value + 1), NULL, NULL, NULL, NULL, 0, NULL,
                ?, NULL, ?, ?, 'QUEUED', 'FAILED', NULL, NULL, NULL, NULL,
                'WORKER_FAILED', 'worker execution failed', 1
            FROM sequence
            """,
            (count, timestamp, timestamp, timestamp),
        )


def _seed_same_book_terminal_history(
    repository: RunRepository,
    count: int,
    *,
    book_id: str = "book-alpha",
) -> None:
    timestamp = "2026-08-20T08:00:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            WITH RECURSIVE sequence(value) AS (
                VALUES (0)
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value + 1 < ?
            )
            INSERT INTO snapshot_runs (
                run_id, run_kind, idempotency_identity, request_fingerprint,
                client_idempotency_key_digest, book_id, captured_generation,
                expected_active_snapshot_id, expected_active_pointer_version,
                target_cut_utc, requested_at_utc, started_at_utc, updated_at_utc,
                finished_at_utc, run_stage, run_outcome,
                cancel_requested_at_utc, candidate_snapshot_id,
                published_snapshot_id, result_json, error_code, error_message, version
            )
            SELECT
                printf('run_book_perf_%016d', value), 'ANALYTICAL_SNAPSHOT',
                printf('%064x', value + 1000000), printf('%064x', value + 1000000),
                NULL, ?, 1, NULL, 0, ?, ?, NULL, ?, ?, 'QUEUED', 'FAILED',
                NULL, NULL, NULL, NULL, 'WORKER_FAILED', 'worker execution failed', 1
            FROM sequence
            """,
            (count, book_id, timestamp, timestamp, timestamp, timestamp),
        )


def _seed_recovery_event_history(
    repository: RunRepository,
    count: int,
    *,
    book_id: str,
    rejected_snapshot_id: str,
) -> None:
    if count < 1:
        return
    timestamp = "2026-08-20T08:05:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        updated = connection.execute(
            """
            UPDATE active_snapshots
            SET snapshot_id = NULL, book_generation = NULL,
                pointer_version = pointer_version + 1, updated_at_utc = ?
            WHERE book_id = ? AND snapshot_id = ? AND pointer_version = 1
            """,
            (timestamp, book_id, rejected_snapshot_id),
        )
        assert updated.rowcount == 1
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (?, ?, 1, 'REMOVED', NULL,
                      '{"failures":[],"omitted_count":0}', ?)
            """,
            (book_id, rejected_snapshot_id, timestamp),
        )
        connection.execute(
            """
            WITH RECURSIVE sequence(value) AS (
                SELECT 1 WHERE ? > 1
                UNION ALL SELECT value + 1 FROM sequence WHERE value + 1 < ?
            )
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            )
            SELECT ?, ?, 1, 'CAS_LOST', NULL,
                   '{"failures":[],"omitted_count":0}', ?
            FROM sequence
            """,
            (count, count, book_id, rejected_snapshot_id, timestamp),
        )


def _seed_publication_chain(
    repository: RunRepository,
    count: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    timestamp = "2026-08-20T08:00:00.000000Z"
    snapshot_ids = tuple(f"{index + 1_000_000:064x}" for index in range(count))
    run_ids = tuple(f"run_chain_{index:08d}" for index in range(count))
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index, (snapshot_id, run_id) in enumerate(zip(snapshot_ids, run_ids)):
            fingerprint = f"{index + 2_000_000:064x}"
            request = _new_run(
                run_id=run_id,
                request_fingerprint=fingerprint,
                client_idempotency_key=None,
            )
            identity = RunRepository._idempotency_identity(request, 1, None)
            expected_snapshot_id = None if index == 0 else snapshot_ids[index - 1]
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
                    ?, 'ANALYTICAL_SNAPSHOT', ?, ?, NULL, 'book-alpha', 1,
                    ?, ?, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                    NULL, ?, ?, NULL, NULL, NULL, 1
                )
                """,
                (
                    run_id,
                    identity,
                    fingerprint,
                    expected_snapshot_id,
                    index,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    snapshot_id,
                    snapshot_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO snapshot_manifests (
                    snapshot_id, run_id, book_id, book_generation, snapshot_status,
                    schema_version, hash_algorithm, manifest_relpath, envelope_sha256,
                    envelope_byte_length, published_at_utc
                ) VALUES (?, ?, 'book-alpha', 1, 'BLESSED',
                    'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{snapshot_id[:2]}/{snapshot_id}.json",
                    f"{index + 3_000_000:064x}",
                    timestamp,
                ),
            )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = ?, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (snapshot_ids[-1], count, timestamp),
        foreign_keys=True,
    )
    return snapshot_ids, run_ids


def _publish_two_snapshot_chain(repository: RunRepository):
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    published_first = repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K1A",
        snapshot_id=SNAPSHOT_B,
    )
    published_second = repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    return first, published_first, second, published_second


def _roll_pointer_register_back_to_first_publication(
    repository: RunRepository,
) -> None:
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = 1, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (SNAPSHOT_A, "2026-08-20T08:03:00.000000Z"),
        foreign_keys=True,
    )


def _execute_without_named_trigger(
    repository: RunRepository,
    trigger_name: str,
    statement: str,
    parameters: tuple[object, ...],
    *,
    ignore_check_constraints: bool = False,
) -> None:
    _execute_without_named_triggers(
        repository,
        (trigger_name,),
        statement,
        parameters,
        ignore_check_constraints=ignore_check_constraints,
    )


def _execute_without_named_triggers(
    repository: RunRepository,
    trigger_names: tuple[str, ...],
    statement: str,
    parameters: tuple[object, ...],
    *,
    ignore_check_constraints: bool = False,
    foreign_keys: bool = False,
) -> None:
    with sqlite3.connect(
        repository.database_path,
        isolation_level=None,
    ) as connection:
        if ignore_check_constraints:
            connection.execute("PRAGMA ignore_check_constraints = ON")
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        placeholders = ", ".join("?" for _ in trigger_names)
        trigger_rows = connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            f"WHERE type = 'trigger' AND name IN ({placeholders})",
            trigger_names,
        ).fetchall()
        found_names = {str(row[0]) for row in trigger_rows}
        assert found_names == set(trigger_names)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for trigger_name in trigger_names:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(statement, parameters)
            trigger_sql_by_name = {
                str(row[0]): str(row[1]) for row in trigger_rows
            }
            for trigger_name in trigger_names:
                connection.execute(trigger_sql_by_name[trigger_name])
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


_MANIFEST_UPDATE_BYPASS = (
    "manifest_update_immutable",
    "manifest_selected_snapshot_stays_blessed",
)
_TERMINAL_RUN_UPDATE_BYPASS = ("snapshot_run_terminal_update_immutable",)
_TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS = (
    "snapshot_run_terminal_update_immutable",
    "snapshot_run_allocation_immutable",
)
_RECOVERY_EVENT_UPDATE_BYPASS = ("recovery_event_update_immutable",)
_ACTIVE_POINTER_UPDATE_BYPASS = ("active_pointer_transition_guard",)
_RECOVERY_EVENT_INSERT_CAUSAL_BYPASS = (
    "recovery_event_pointer_causal_on_insert",
)


def _insert_or_replace_run_from_existing(
    connection: sqlite3.Connection,
    source_run_id: str,
    *,
    overrides: dict[str, object],
    explicit_rowid: int | None = None,
) -> None:
    connection.row_factory = sqlite3.Row
    source = connection.execute(
        "SELECT * FROM snapshot_runs WHERE run_id = ?",
        (source_run_id,),
    ).fetchone()
    assert source is not None
    columns = tuple(source.keys())
    values = {column: source[column] for column in columns}
    values.update(overrides)
    insert_columns = columns
    parameters: tuple[object, ...] = tuple(values[column] for column in columns)
    if explicit_rowid is not None:
        insert_columns = ("rowid", *insert_columns)
        parameters = (explicit_rowid, *parameters)
    placeholders = ", ".join("?" for _ in insert_columns)
    connection.execute(
        f"INSERT OR REPLACE INTO snapshot_runs ({', '.join(insert_columns)}) "
        f"VALUES ({placeholders})",
        parameters,
    )


def _insert_raw_recovery_event(
    connection: sqlite3.Connection,
    *,
    rejected_snapshot_id: str,
    selected_snapshot_id: str | None = None,
    expected_pointer_version: int = 1,
    recorded_at_utc: str = "2026-08-20T08:04:00.000000Z",
    event_sequence: int | None = None,
    replace: bool = False,
) -> None:
    columns = (
        "book_id",
        "rejected_snapshot_id",
        "expected_pointer_version",
        "resolution_action",
        "selected_snapshot_id",
        "detail_json",
        "recorded_at_utc",
    )
    parameters: tuple[object, ...] = (
        "book-alpha",
        rejected_snapshot_id,
        expected_pointer_version,
        "CAS_LOST",
        selected_snapshot_id,
        '{"failures":[],"omitted_count":0}',
        recorded_at_utc,
    )
    if event_sequence is not None:
        columns = ("event_sequence", *columns)
        parameters = (event_sequence, *parameters)
    insert_verb = "INSERT OR REPLACE" if replace else "INSERT"
    connection.execute(
        f"{insert_verb} INTO snapshot_recovery_events ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        parameters,
    )


def _damage_live_identity_index(repository: RunRepository) -> None:
    index_name = "one_live_idempotency_identity"
    with sqlite3.connect(repository.database_path) as connection:
        saved_index = connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_schema WHERE name = ?",
            (index_name,),
        ).fetchone()
        assert saved_index is not None
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute("DELETE FROM sqlite_schema WHERE name = ?", (index_name,))
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")

    repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I8X",
            book_id=None,
            client_idempotency_key=None,
        ),
        now=T0,
    )

    with sqlite3.connect(repository.database_path) as connection:
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "INSERT INTO sqlite_schema(type, name, tbl_name, rootpage, sql) "
            "VALUES (?, ?, ?, ?, ?)",
            saved_index,
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")


class _VmBudgetRunRepository(RunRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.vm_callbacks = 0

    def _count_vm_steps(self) -> int:
        self.vm_callbacks += 1
        return 0

    def _open_connection(
        self,
        *,
        configure_wal: bool,
        timeout_ms: int = 5_000,
    ) -> sqlite3.Connection:
        connection = super()._open_connection(
            configure_wal=configure_wal,
            timeout_ms=timeout_ms,
        )
        connection.set_progress_handler(self._count_vm_steps, 100)
        return connection

    def reset_vm_callbacks(self) -> None:
        self.vm_callbacks = 0


class _TracingVmRunRepository(_VmBudgetRunRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.statements: list[str] = []

    def _open_connection(
        self,
        *,
        configure_wal: bool,
        timeout_ms: int = 5_000,
    ) -> sqlite3.Connection:
        connection = super()._open_connection(
            configure_wal=configure_wal,
            timeout_ms=timeout_ms,
        )
        connection.set_trace_callback(self.statements.append)
        return connection


class _InitializationRaceConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        role: str,
        first_locked: threading.Event,
        second_attempting: threading.Event,
        second_locked: threading.Event,
    ) -> None:
        self._connection = connection
        self._role = role
        self._first_locked = first_locked
        self._second_attempting = second_attempting
        self._second_locked = second_locked

    def execute(self, statement: str, parameters: tuple[object, ...] = ()):
        normalized = " ".join(statement.upper().split())
        if normalized == "BEGIN IMMEDIATE":
            if self._role == "first":
                cursor = self._connection.execute(statement, parameters)
                self._first_locked.set()
                if not self._second_attempting.wait(timeout=5):
                    raise AssertionError("second initializer did not attempt its write lock")
                return cursor
            self._second_attempting.set()
            cursor = self._connection.execute(statement, parameters)
            self._second_locked.set()
            time.sleep(0.1)
            return cursor
        return self._connection.execute(statement, parameters)

    def commit(self) -> None:
        self._connection.commit()
        if self._role == "first":
            if not self._second_locked.wait(timeout=5):
                raise AssertionError("second initializer did not acquire its write lock")

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class _CoordinatedInitializationRepository(RunRepository):
    def __init__(
        self,
        root: Path,
        *,
        role: str,
        first_locked: threading.Event,
        second_attempting: threading.Event,
        second_locked: threading.Event,
    ) -> None:
        super().__init__(root)
        self._role = role
        self._first_locked = first_locked
        self._second_attempting = second_attempting
        self._second_locked = second_locked

    def _open_connection(
        self,
        *,
        configure_wal: bool,
        timeout_ms: int = 5_000,
    ):
        connection = super()._open_connection(
            configure_wal=configure_wal,
            timeout_ms=timeout_ms,
        )
        return _InitializationRaceConnection(
            connection,
            role=self._role,
            first_locked=self._first_locked,
            second_attempting=self._second_attempting,
            second_locked=self._second_locked,
        )


class _TrackedWalConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        repository: _BusyWalRepository,
    ) -> None:
        self._connection = connection
        self._repository = repository
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._repository.closed_connections += 1
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class _BusyWalRepository(RunRepository):
    def __init__(
        self,
        root: Path,
        *,
        failures_before_success: int | None,
        deadline_seconds: float = 2.0,
    ) -> None:
        super().__init__(root)
        self._WAL_ENABLE_TIMEOUT_SECONDS = deadline_seconds
        self.failures_remaining = failures_before_success
        self.wal_attempts = 0
        self.opened_connections = 0
        self.closed_connections = 0
        self.fake_time = 0.0
        self.sleep_durations: list[float] = []

    def _open_connection(
        self,
        *,
        configure_wal: bool,
        timeout_ms: int = 5_000,
    ):
        connection = super()._open_connection(
            configure_wal=configure_wal,
            timeout_ms=timeout_ms,
        )
        self.opened_connections += 1
        return _TrackedWalConnection(connection, self)

    def _request_wal_mode(self, connection: sqlite3.Connection) -> str:
        self.wal_attempts += 1
        if self.failures_remaining is None or self.failures_remaining > 0:
            if self.failures_remaining is not None:
                self.failures_remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        return super()._request_wal_mode(connection)

    def _wal_now(self) -> float:
        return self.fake_time

    def _wal_sleep(self, duration_seconds: float) -> None:
        self.sleep_durations.append(duration_seconds)
        self.fake_time += duration_seconds


class _NonOperationalWalErrorRepository(_BusyWalRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root, failures_before_success=0)

    def _request_wal_mode(self, connection: sqlite3.Connection) -> str:
        self.wal_attempts += 1
        raise sqlite3.DatabaseError("database disk image is malformed")


def test_initialize_migrates_empty_root_idempotently_and_reopens(tmp_path: Path) -> None:
    # Break caught: initialization that depends on a pre-existing snapshots directory,
    # reapplies migrations, or fails to preserve durable rows on reopen.
    repository = RunRepository(tmp_path)
    assert not repository.database_path.exists()

    repository.initialize()
    repository.initialize()
    repository.advance_book_head(
        book_id="book-alpha",
        generation=1,
        canonical_book_ref=BOOK_REF_1,
        now=T0,
    )

    reopened = RunRepository(tmp_path)
    reopened.initialize()
    assert reopened.database_path == tmp_path / "snapshots" / "runs.sqlite3"
    assert reopened.get_book_head("book-alpha").generation == 1

    with sqlite3.connect(reopened.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "book_heads",
        "snapshot_runs",
        "snapshot_manifests",
        "active_snapshots",
        "snapshot_recovery_events",
    } <= tables


def test_every_repository_connection_applies_required_pragmas(tmp_path: Path) -> None:
    # Break caught: a callback/thread connection silently running without FK, FULL sync,
    # bounded lock wait, or the persistent WAL journal.
    repository = _repository(tmp_path)

    pragmas = repository.inspect_connection_pragmas()

    assert pragmas.foreign_keys == 1
    assert pragmas.journal_mode == "wal"
    assert pragmas.synchronous == 2
    assert pragmas.busy_timeout_ms == 5_000


def test_initialize_maps_unavailable_database_root_to_typed_failure(
    tmp_path: Path,
) -> None:
    # Break caught: raw filesystem/SQLite exceptions leaking across the repository boundary.
    blocked_root = tmp_path / "blocked-root"
    blocked_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RunDatabaseError):
        RunRepository(blocked_root).initialize()


def test_unexpected_sqlite_read_failure_is_mapped_to_typed_database_error(
    tmp_path: Path,
) -> None:
    # Break caught: raw sqlite exceptions escaping from short-lived read connections.
    repository = _repository(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("DROP TABLE snapshot_runs")

    with pytest.raises(RunDatabaseError):
        repository.list_runs()


def test_concurrent_thread_initializers_converge_on_one_complete_schema(
    tmp_path: Path,
) -> None:
    # Break caught: callers reading user_version before the migration write lock and racing
    # unconditional CREATE TABLE statements.
    barrier = threading.Barrier(16)
    failures: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait()
            RunRepository(tmp_path).initialize()
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    threads = [threading.Thread(target=initialize) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert _raw_journal_mode(RunRepository(tmp_path)) == "wal"


def test_concurrent_process_initializers_converge_on_one_complete_schema(
    tmp_path: Path,
) -> None:
    # Break caught: first-start safety existing only inside one Python process.
    code = (
        "from pathlib import Path; "
        "from quantmind.snapshots.run_repository import RunRepository; "
        f"RunRepository(Path({str(tmp_path)!r})).initialize()"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    completed = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0] * len(processes), completed
    RunRepository(tmp_path).initialize()


def test_initializer_waits_for_next_serialized_writer_before_enabling_wal(
    tmp_path: Path,
) -> None:
    # Break caught: eight tight post-commit WAL retries exhausting under the next initializer.
    first_locked = threading.Event()
    second_attempting = threading.Event()
    second_locked = threading.Event()
    failures: list[BaseException] = []

    def initialize(role: str) -> None:
        try:
            repository = _CoordinatedInitializationRepository(
                tmp_path,
                role=role,
                first_locked=first_locked,
                second_attempting=second_attempting,
                second_locked=second_locked,
            )
            repository.initialize()
            if _raw_journal_mode(repository) != "wal":
                raise AssertionError("initializer returned before WAL was durable")
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    first = threading.Thread(target=initialize, args=("first",))
    first.start()
    assert first_locked.wait(timeout=5)
    second = threading.Thread(target=initialize, args=("second",))
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert _raw_journal_mode(RunRepository(tmp_path)) == "wal"


def test_initializer_reopens_and_outwaits_more_than_eight_wal_busy_results(
    tmp_path: Path,
) -> None:
    # Break caught: a fixed eight-attempt loop failing before a bounded lock clears.
    repository = _BusyWalRepository(tmp_path, failures_before_success=12)

    repository.initialize()

    assert repository.wal_attempts > 8
    assert repository.sleep_durations
    assert repository.opened_connections == repository.closed_connections
    assert _raw_journal_mode(RunRepository(tmp_path)) == "wal"


def test_initializer_always_busy_wal_failure_is_typed_bounded_and_closes_handles(
    tmp_path: Path,
) -> None:
    # Protective regression: the deadline cannot leak handles or escape as raw SQLite errors.
    repository = _BusyWalRepository(
        tmp_path,
        failures_before_success=None,
        deadline_seconds=0.05,
    )
    with pytest.raises(RunDatabaseError):
        repository.initialize()

    assert repository.fake_time >= repository._WAL_ENABLE_TIMEOUT_SECONDS
    assert repository.sleep_durations
    assert repository.opened_connections == repository.closed_connections


@pytest.mark.parametrize("damage", ["version_only", "missing_index"])
def test_claimed_v1_partial_or_malformed_schema_fails_typed(
    tmp_path: Path, damage: str
) -> None:
    # Break caught: trusting user_version without validating catalog shape.
    repository = RunRepository(tmp_path)
    repository.database_path.parent.mkdir(parents=True)
    if damage == "version_only":
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute("PRAGMA user_version = 1")
    else:
        repository.initialize()
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute("DROP INDEX one_live_snapshot_per_book_generation")

    with pytest.raises(RunDatabaseError):
        repository.initialize()


def test_claimed_v1_with_weakened_table_constraint_is_rejected(tmp_path: Path) -> None:
    # Break caught: matching PRAGMA shapes hiding a weakened v1 CHECK constraint.
    repository = _repository(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            """
            UPDATE sqlite_master
            SET sql = replace(sql, 'version >= 1', 'version >= 0')
            WHERE type = 'table' AND name = 'book_heads'
            """
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


def _revalidate_publication_result(
    result: PublicationResultV1, *, json_round_trip: bool
) -> PublicationResultV1:
    if json_round_trip:
        return PublicationResultV1.model_validate_json(result.model_dump_json())
    return PublicationResultV1.model_validate(
        result.model_dump(mode="python", warnings=False)
    )


@pytest.mark.parametrize("json_round_trip", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "published_flag",
        "already_without_publication",
        "rejection_on_success",
        "foreign_publication_run",
        "foreign_publication_book",
        "foreign_publication_generation",
        "foreign_publication_snapshot",
        "foreign_publication_time",
        "foreign_active_book",
        "impossible_active_successor",
    ],
)
def test_publication_result_rejects_bypassed_cross_record_incoherence(
    tmp_path: Path,
    mutation: str,
    json_round_trip: bool,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    candidate = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    result = repository.commit_publication(
        candidate.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=candidate.version,
        now=T3,
    )
    publication = result.publication
    active = result.active
    assert publication is not None
    assert active is not None

    if mutation == "published_flag":
        forged = result.model_copy(update={"published": False})
    elif mutation == "already_without_publication":
        forged = result.model_copy(
            update={"publication": None, "published": False, "already_published": True}
        )
    elif mutation == "rejection_on_success":
        forged = result.model_copy(
            update={"rejection_code": RunErrorCode.STALE_ACTIVE_POINTER}
        )
    elif mutation == "foreign_publication_run":
        forged = result.model_copy(
            update={
                "publication": publication.model_copy(
                    update={"run_id": "run_01J5X5S8J5J8P7KQ4Y0T3N6ZZZ"}
                )
            }
        )
    elif mutation == "foreign_publication_book":
        forged = result.model_copy(
            update={"publication": publication.model_copy(update={"book_id": "book-beta"})}
        )
    elif mutation == "foreign_publication_generation":
        forged = result.model_copy(
            update={"publication": publication.model_copy(update={"book_generation": 2})}
        )
    elif mutation == "foreign_publication_snapshot":
        forged_publication = publication.model_copy(
            update={
                "snapshot_id": SNAPSHOT_B,
                "manifest_relpath": (
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{SNAPSHOT_B[:2]}/{SNAPSHOT_B}.json"
                ),
            }
        )
        forged = result.model_copy(update={"publication": forged_publication})
    elif mutation == "foreign_publication_time":
        forged = result.model_copy(
            update={"publication": publication.model_copy(update={"published_at_utc": T4})}
        )
    elif mutation == "foreign_active_book":
        forged = result.model_copy(
            update={"active": active.model_copy(update={"book_id": "book-beta"})}
        )
    else:
        forged = result.model_copy(
            update={
                "active": active.model_copy(
                    update={"snapshot_id": SNAPSHOT_B, "book_generation": 1}
                )
            }
        )

    with pytest.raises(ValueError):
        _revalidate_publication_result(forged, json_round_trip=json_round_trip)


@pytest.mark.parametrize("json_round_trip", [False, True])
@pytest.mark.parametrize(
    "error_code",
    [
        RunErrorCode.WORKER_FAILED,
        RunErrorCode.STALE_BOOK_GENERATION,
        RunErrorCode.STALE_ACTIVE_POINTER,
    ],
)
def test_publication_result_rejects_open_or_candidate_free_rejection_shapes(
    tmp_path: Path,
    error_code: RunErrorCode,
    json_round_trip: bool,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = repository.create_or_join(_new_run(), now=T1).record
    failed = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T2,
    )
    forged_run = failed.model_copy(
        update={
            "error_code": error_code,
            "error_message": {
                RunErrorCode.WORKER_FAILED: "worker execution failed",
                RunErrorCode.STALE_BOOK_GENERATION: (
                    "canonical book generation changed before publication"
                ),
                RunErrorCode.STALE_ACTIVE_POINTER: (
                    "active snapshot pointer changed before publication"
                ),
            }[error_code],
        }
    )
    forged = PublicationResultV1.model_construct(
        run=forged_run,
        publication=None,
        active=None,
        published=False,
        already_published=False,
        rejection_code=error_code,
    )

    with pytest.raises(ValueError):
        _revalidate_publication_result(forged, json_round_trip=json_round_trip)


def test_resolve_publication_result_is_one_bounded_read_without_history_scan(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    candidate = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    committed = repository.commit_publication(
        candidate.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=candidate.version,
        now=T3,
    )

    class CountingReadRepository(RunRepository):
        read_transactions = 0

        @contextmanager
        def _read_connection(self):
            self.read_transactions += 1
            with super()._read_connection() as connection:
                yield connection

        def list_publications(self, *args, **kwargs):
            raise AssertionError("run-scoped resolution must not scan publication history")

    reopened = CountingReadRepository(tmp_path)
    reopened.initialize()
    reopened.read_transactions = 0

    resolved = reopened.resolve_publication_result(
        candidate.run_id, already_published=False
    )

    assert resolved == committed
    assert reopened.read_transactions == 1
    with pytest.raises(TypeError, match="boolean"):
        reopened.resolve_publication_result(candidate.run_id, already_published=1)


def test_publication_result_resolution_has_constant_valid_chain_work(
    tmp_path: Path,
) -> None:
    costs: dict[int, tuple[int, int]] = {}
    for publication_count in (10, 100, 1_000):
        repository = _TracingVmRunRepository(
            tmp_path / f"chain-{publication_count}"
        )
        repository.initialize()
        _snapshot_ids, run_ids = _seed_publication_chain(
            repository, publication_count
        )
        repository.statements.clear()
        repository.reset_vm_callbacks()

        result = repository.resolve_publication_result(
            run_ids[-1], already_published=True
        )

        selects = tuple(
            statement
            for statement in repository.statements
            if statement.lstrip().upper().startswith("SELECT")
        )
        assert result.run.run_id == run_ids[-1]
        costs[publication_count] = (repository.vm_callbacks, len(selects))

    vm_callbacks = tuple(cost[0] for cost in costs.values())
    query_counts = tuple(cost[1] for cost in costs.values())
    assert query_counts == (15, 15, 15)
    assert max(vm_callbacks) <= 2
    assert max(vm_callbacks) <= min(vm_callbacks) + 1


@pytest.mark.parametrize(
    "reserved_code",
    [
        RunErrorCode.CANCELLED_BY_USER,
        RunErrorCode.STALE_BOOK_GENERATION,
        RunErrorCode.STALE_ACTIVE_POINTER,
    ],
)
def test_generic_failure_api_cannot_mint_publication_owned_evidence(
    tmp_path: Path,
    reserved_code: RunErrorCode,
) -> None:
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=reserved_code.value),
        now=T0,
    ).record

    with pytest.raises(ValueError, match="publication|cancellation"):
        RunFailureV1(code=reserved_code)
    forged = RunFailureV1.model_construct(code=reserved_code)
    with pytest.raises(ValueError, match="publication|cancellation"):
        repository.mark_failed(
            run.run_id,
            forged,
            expected_version=run.version,
            now=T1,
        )


@pytest.mark.parametrize("head_advanced", [False, True])
def test_stale_generation_resolution_requires_a_newer_current_head(
    tmp_path: Path,
    head_advanced: bool,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    terminal = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T3,
    )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_UPDATE_BYPASS,
        "UPDATE snapshot_runs SET error_code = ?, error_message = ? WHERE run_id = ?",
        (
            RunErrorCode.STALE_BOOK_GENERATION.value,
            "canonical book generation changed before publication",
            terminal.run_id,
        ),
    )
    if head_advanced:
        repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T4)

    if head_advanced:
        assert (
            repository.get(run.run_id).error_code
            is RunErrorCode.STALE_BOOK_GENERATION
        )
        resolved = repository.resolve_publication_result(
            run.run_id, already_published=False
        )
        assert resolved.rejection_code is RunErrorCode.STALE_BOOK_GENERATION
    else:
        with pytest.raises(RunDatabaseError, match="generation"):
            repository.get(run.run_id)
        with pytest.raises(RunDatabaseError, match="generation"):
            repository.resolve_publication_result(
                run.run_id, already_published=False
            )


def test_resolve_publication_result_refuses_a_non_catalog_run_with_typed_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    running = _publishing_run(repository, snapshot_id=SNAPSHOT_A)

    with pytest.raises(RunDatabaseError, match="publication result"):
        repository.resolve_publication_result(
            running.run_id, already_published=False
        )


def test_publication_result_allows_cancellation_before_candidate_attachment(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = repository.create_or_join(_new_run(), now=T1).record
    run = repository.claim_start(
        run.run_id, expected_version=run.version, now=T1
    )
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        run = repository.advance_stage(
            run.run_id, stage, expected_version=run.version, now=T2
        )
    requested = repository.request_cancel(run.run_id, now=T3)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=requested.version,
        now=T3,
    )

    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.candidate_snapshot_id is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.publication is None


def test_publication_rejection_refuses_a_later_pointer_with_pre_request_clock(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    published = repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    assert published.active is not None
    rejected_run = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6ZZA",
        snapshot_id=SNAPSHOT_B,
        now=T3,
    )
    repository.request_cancel(rejected_run.run_id, now=T4)
    rejected = repository.commit_publication(
        rejected_run.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=rejected_run.version,
        now=T4,
    )
    assert rejected.active is not None
    forged = rejected.model_copy(
        update={
            "active": rejected.active.model_copy(
                update={"pointer_version": 2, "updated_at_utc": T0}
            )
        }
    )

    with pytest.raises(ValueError, match="clock"):
        PublicationResultV1.model_validate(
            forged.model_dump(mode="python", warnings=False)
        )


def test_stale_pointer_rejection_cannot_claim_the_captured_predecessor_is_current(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    published = repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    assert published.active is not None
    stale = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6ZZB",
        snapshot_id=SNAPSHOT_C,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda _snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )
    rejected = repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=stale.version,
        now=T5,
    )
    assert rejected.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert rejected.active is None
    forged = rejected.model_copy(update={"active": published.active})

    with pytest.raises(ValueError, match="later pointer"):
        PublicationResultV1.model_validate(
            forged.model_dump(mode="python", warnings=False)
        )


@pytest.mark.parametrize(
    "object_ddl",
    [
        """
        CREATE TRIGGER hostile_after_run_update
        AFTER UPDATE ON snapshot_runs
        BEGIN
            DELETE FROM book_heads;
        END
        """,
        "CREATE VIEW hostile_run_view AS SELECT run_id FROM snapshot_runs",
    ],
)
def test_claimed_v1_rejects_unexpected_noninternal_sqlite_objects(
    tmp_path: Path, object_ddl: str
) -> None:
    # Break caught: exact-v1 attestation ignoring executable triggers or queryable views.
    repository = _repository(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(object_ddl)

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


@pytest.mark.parametrize(
    "object_ddl",
    [
        "CREATE TABLE sqliteXhostile_table (value TEXT)",
        "CREATE INDEX sqliteXhostile_index ON book_heads(generation)",
        """
        CREATE TRIGGER sqliteXhostile_trigger
        AFTER UPDATE ON snapshot_runs
        BEGIN
            DELETE FROM book_heads;
        END
        """,
        "CREATE VIEW sqliteXhostile_view AS SELECT run_id FROM snapshot_runs",
    ],
)
def test_claimed_v1_rejects_sqlite_like_disguised_persistent_objects(
    tmp_path: Path, object_ddl: str
) -> None:
    # Break caught: SQL LIKE treating the underscore in sqlite_ as a wildcard.
    repository = _repository(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(object_ddl)

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


@pytest.mark.parametrize("object_kind", ["table", "index", "trigger", "view"])
def test_claimed_v1_rejects_writable_schema_sqlite_prefixed_objects(
    tmp_path: Path, object_kind: str
) -> None:
    # Break caught: blanket sqlite_ exclusion trusting attacker-injected persistent objects.
    repository = _repository(tmp_path)
    definitions = {
        "table": "CREATE TABLE hostile_table (value TEXT)",
        "index": "CREATE INDEX hostile_index ON book_heads(generation)",
        "trigger": (
            "CREATE TRIGGER hostile_trigger AFTER UPDATE ON snapshot_runs "
            "BEGIN DELETE FROM book_heads; END"
        ),
        "view": "CREATE VIEW hostile_view AS SELECT run_id FROM snapshot_runs",
    }
    ordinary_name = f"hostile_{object_kind}"
    sqlite_name = f"sqlite_hostile_{object_kind}"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(definitions[object_kind])
        connection.execute("PRAGMA writable_schema = ON")
        if object_kind in {"table", "view"}:
            connection.execute(
                """
                UPDATE sqlite_master
                SET name = ?, tbl_name = ?, sql = replace(sql, ?, ?)
                WHERE name = ?
                """,
                (
                    sqlite_name,
                    sqlite_name,
                    ordinary_name,
                    sqlite_name,
                    ordinary_name,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE sqlite_master
                SET name = ?, sql = replace(sql, ?, ?)
                WHERE name = ?
                """,
                (sqlite_name, ordinary_name, sqlite_name, ordinary_name),
            )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


def test_book_generation_is_monotonic_and_same_generation_is_immutable(
    tmp_path: Path,
) -> None:
    # Break caught: an older or conflicting canonical book replacing current durable truth.
    repository = _repository(tmp_path)
    first = repository.advance_book_head(
        "book-alpha", 1, BOOK_REF_1, now=T0
    )
    repeated = repository.advance_book_head(
        "book-alpha", 1, BOOK_REF_1, now=T1
    )
    second = repository.advance_book_head(
        "book-alpha", 2, BOOK_REF_2, now=T2
    )

    assert first.version == 1
    assert repeated == first
    assert second.generation == 2
    assert second.version == 2
    with pytest.raises(GenerationRegressionError):
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T3)
    with pytest.raises(GenerationRegressionError):
        repository.advance_book_head("book-alpha", 2, BOOK_REF_1, now=T3)


def test_create_or_join_commits_queued_row_and_captures_generation_and_pointer(
    tmp_path: Path,
) -> None:
    # Break caught: returning an enqueue token before durable truth exists, or reading the
    # book/pointer after allocation in a different transaction.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 7, BOOK_REF_1, now=T0)

    result = repository.create_or_join(_new_run(), now=T1)
    reopened = RunRepository(tmp_path)
    reopened.initialize()
    persisted = reopened.get(result.record.run_id)

    assert result.created is True
    assert persisted.run_stage is RunStage.QUEUED
    assert persisted.run_outcome is RunOutcome.RUNNING
    assert persisted.book_id == "book-alpha"
    assert persisted.captured_generation == 7
    assert persisted.expected_active_snapshot_id is None
    assert persisted.expected_active_pointer_version == 0
    assert len(persisted.idempotency_identity) == 64
    assert persisted.requested_at_utc == T1
    assert persisted.version == 1


def test_create_or_join_is_deterministic_under_two_thread_race(tmp_path: Path) -> None:
    # Break caught: process-local duplicate suppression allowing two live durable rows.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    barrier = threading.Barrier(2)
    results = []
    errors: list[BaseException] = []

    def allocate(run_id: str) -> None:
        try:
            barrier.wait()
            results.append(
                repository.create_or_join(_new_run(run_id), now=T1)
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [
        threading.Thread(target=allocate, args=("run_01J5X5S8J5J8P7KQ4Y0T3N6M9A",)),
        threading.Thread(target=allocate, args=("run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 2
    winning_ids = {result.record.run_id for result in results}
    assert len(winning_ids) == 1
    assert winning_ids <= {
        "run_01J5X5S8J5J8P7KQ4Y0T3N6M9A",
        "run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
    }
    assert sorted(result.created for result in results) == [False, True]
    assert len(repository.list_runs(book_id="book-alpha")) == 1


def test_incompatible_live_request_for_same_book_generation_is_refused(
    tmp_path: Path,
) -> None:
    # Break caught: two differently configured runs racing to publish from the same generation.
    repository = _repository(tmp_path)
    _allocated(repository)

    with pytest.raises(IncompatibleLiveRunError):
        repository.create_or_join(
            _new_run(
                "run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
                request_fingerprint="b" * 64,
            ),
            now=T2,
        )


def test_terminal_retry_with_same_identity_creates_fresh_run(tmp_path: Path) -> None:
    # Break caught: a terminal row permanently pinning an idempotency identity.
    repository = _repository(tmp_path)
    first = _allocated(repository)
    failed = repository.mark_failed(
        first.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=first.version,
        now=T2,
    )

    retried = repository.create_or_join(
        _new_run("run_01J5X5S8J5J8P7KQ4Y0T3N6M9B"), now=T3
    )

    assert failed.run_outcome is RunOutcome.FAILED
    assert retried.created is True
    assert retried.record.run_id.endswith("M9B")


def test_missing_book_head_refuses_snapshot_allocation(tmp_path: Path) -> None:
    # Break caught: snapshot work beginning without an immutable canonical-book generation.
    repository = _repository(tmp_path)
    with pytest.raises(RunNotFoundError):
        repository.create_or_join(_new_run(), now=T1)


def test_claim_and_each_exact_adjacent_stage_transition(tmp_path: Path) -> None:
    # Break caught: a legal production stage becoming unreachable.
    repository = _repository(tmp_path)
    record = _allocated(repository)

    record = repository.claim_start(record.run_id, expected_version=1, now=T2)
    assert record.run_stage is RunStage.INGESTING
    assert record.started_at_utc == T2
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        record = repository.advance_stage(
            record.run_id,
            stage,
            expected_version=record.version,
            now=T3,
        )
        assert record.run_stage is stage


@pytest.mark.parametrize(
    ("current", "attempted"),
    [
        (RunStage.QUEUED, RunStage.RECONCILING),
        (RunStage.INGESTING, RunStage.VALIDATING),
        (RunStage.RECONCILING, RunStage.INGESTING),
        (RunStage.VALIDATING, RunStage.VALIDATING),
    ],
)
def test_stage_transition_rejects_skip_backward_and_repeat(
    tmp_path: Path, current: RunStage, attempted: RunStage
) -> None:
    # Break caught: a skipped, backward, or repeated stage being accepted as progress.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    if current is not RunStage.QUEUED:
        record = repository.claim_start(record.run_id, expected_version=record.version, now=T2)
        for stage in (RunStage.RECONCILING, RunStage.VALIDATING):
            if record.run_stage is current:
                break
            record = repository.advance_stage(
                record.run_id, stage, expected_version=record.version, now=T2
            )

    with pytest.raises(IllegalRunTransitionError):
        repository.advance_stage(
            record.run_id, attempted, expected_version=record.version, now=T3
        )


def test_stage_transition_rejects_stale_version(tmp_path: Path) -> None:
    # Break caught: an old callback advancing over newer durable state.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    with pytest.raises(StaleRunVersionError):
        repository.claim_start(record.run_id, expected_version=2, now=T2)


def test_cancel_intent_is_durable_idempotent_and_requires_acknowledgement(
    tmp_path: Path,
) -> None:
    # Break caught: request_cancel lying by terminalizing work before a future/worker ack.
    repository = _repository(tmp_path)
    record = _allocated(repository)

    requested = repository.request_cancel(record.run_id, now=T2)
    repeated = repository.request_cancel(record.run_id, now=T3)

    assert requested.run_outcome is RunOutcome.RUNNING
    assert requested.cancel_requested_at_utc == T2
    assert repeated == requested
    cancelled = repository.acknowledge_cancel(
        record.run_id,
        expected_version=requested.version,
        now=T3,
    )
    assert cancelled.run_outcome is RunOutcome.CANCELLED
    assert cancelled.error_code is RunErrorCode.CANCELLED_BY_USER
    assert cancelled.finished_at_utc == T3


def test_cancel_acknowledgement_without_intent_is_refused(tmp_path: Path) -> None:
    # Break caught: reporting cancellation when no durable cancellation request exists.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    with pytest.raises(IllegalRunTransitionError):
        repository.acknowledge_cancel(
            record.run_id, expected_version=record.version, now=T2
        )


def test_attach_candidate_requires_publishing_and_full_snapshot_id(tmp_path: Path) -> None:
    # Break caught: unvalidated/early candidate identity entering durable evidence.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    with pytest.raises(IllegalRunTransitionError):
        repository.attach_candidate(
            record.run_id, "c" * 64, expected_version=record.version, now=T2
        )
    with pytest.raises(ValueError):
        repository.attach_candidate(
            record.run_id, "short", expected_version=record.version, now=T2
        )


def test_nonpublishing_success_requires_canonical_bounded_json(tmp_path: Path) -> None:
    # Break caught: any caller-supplied JSON bypassing the closed typed result contract.
    repository = _repository(tmp_path)
    for hostile in (
        '{"exit_code":0,"summary":"ok"}',
        '{"a":1,"a":2}',
        '{"value":NaN}',
        '{ "a": 1 }',
        json.dumps({"payload": "x" * 70_000}, separators=(",", ":")),
    ):
        record = repository.create_or_join(
            _new_run(
                run_id=f"run_{len(repository.list_runs()) + 1:026d}",
                book_id=None,
                client_idempotency_key=str(len(repository.list_runs())),
                request_fingerprint=f"{len(repository.list_runs()) + 1:064x}",
            ),
            now=T2,
        ).record
        with pytest.raises(TypeError):
            repository.complete_nonpublishing(
                record.run_id, hostile, expected_version=record.version, now=T3
            )


def test_legacy_result_adapter_is_closed_and_structural() -> None:
    # Break caught: arbitrary executor objects/text becoming durable JSON payloads.
    assert adapt_legacy_result(None).result_code is RunResultCode.EMPTY
    assert adapt_legacy_result(True).boolean_value is True
    assert adapt_legacy_result(42).integer_value == 42
    synced = adapt_legacy_result("synced 3 symbols")
    assert synced.result_code is RunResultCode.SYNC_COMPLETED
    assert synced.integer_value == 3
    for hostile in (
        {"api_key": "TOPSECRET"},
        "../../etc/passwd",
        "VendorResponse(account='U123')",
        object(),
    ):
        with pytest.raises((TypeError, ValueError)):
            adapt_legacy_result(hostile)


def test_nonpublishing_success_persists_only_typed_allowlisted_result(
    tmp_path: Path,
) -> None:
    # Break caught: canonical-but-sensitive arbitrary dictionaries entering result_json.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="sync"), now=T0
    ).record
    typed = RunResultV1(
        schema_version="durable_run_result_v1",
        result_code=RunResultCode.SYNC_COMPLETED,
        boolean_value=None,
        integer_value=3,
        artifact_digest=None,
    )

    completed = repository.complete_nonpublishing(
        run.run_id, typed, expected_version=run.version, now=T1
    )

    assert completed.result == typed
    assert not hasattr(completed, "result_json")
    with sqlite3.connect(repository.database_path) as connection:
        stored = connection.execute(
            "SELECT result_json FROM snapshot_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
    assert stored == (
        '{"artifact_digest":null,"boolean_value":null,"integer_value":3,'
        '"result_code":"SYNC_COMPLETED","schema_version":"durable_run_result_v1"}'
    )


def test_public_pydantic_boundaries_revalidate_construct_and_copy_bypasses(
    tmp_path: Path,
) -> None:
    # Break caught: trusted isinstance checks accepting invalid model_construct/model_copy data.
    repository = _repository(tmp_path)
    invalid_request = _new_run(book_id=None).model_copy(
        update={"request_fingerprint": "short"}
    )
    with pytest.raises(ValueError):
        repository.create_or_join(invalid_request, now=T0)

    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="valid"), now=T0
    ).record
    invalid_result = RunResultV1.model_construct(
        schema_version="durable_run_result_v1",
        result_code=RunResultCode.ARTIFACT_REFERENCE,
        boolean_value=None,
        integer_value=None,
        artifact_digest="short",
    )
    with pytest.raises(ValueError):
        repository.complete_nonpublishing(
            run.run_id,
            invalid_result,
            expected_version=run.version,
            now=T1,
        )

    invalid_failure = RunFailureV1.model_construct(
        code=RunErrorCode.CANCELLED_BY_USER
    )
    with pytest.raises(ValueError):
        repository.mark_failed(
            run.run_id,
            invalid_failure,
            expected_version=run.version,
            now=T1,
        )


def test_all_run_error_codes_round_trip_and_messages_are_safe(tmp_path: Path) -> None:
    # Break caught: enum drift or repository coercion collapsing machine-readable failures.
    repository = _repository(tmp_path)

    for index, code in enumerate(RunErrorCode):
        if code in {
            RunErrorCode.STALE_BOOK_GENERATION,
            RunErrorCode.STALE_ACTIVE_POINTER,
        }:
            # Atomic commit_publication rejection tests own these two codes.
            continue
        run = repository.create_or_join(
            _new_run(
                run_id=f"run_{index:026d}",
                book_id=None,
                client_idempotency_key=f"key-{index}",
                request_fingerprint=f"{index + 1:064x}",
            ),
            now=T0,
        ).record
        if code is RunErrorCode.CANCELLED_BY_USER:
            requested = repository.request_cancel(run.run_id, now=T1)
            failed = repository.acknowledge_cancel(
                run.run_id, expected_version=requested.version, now=T2
            )
        else:
            failed = repository.mark_failed(
                run.run_id,
                RunFailureV1(code=code),
                expected_version=run.version,
                now=T1,
            )
        assert repository.get(run.run_id).error_code is code
        assert failed.error_message is not None

    for unsafe in (
        "line one\nline two",
        "/Users/alice/private/vendor-payload.json",
        "authorization: bearer secret",
        "x" * 1_025,
    ):
        with pytest.raises(ValueError):
            RunFailureV1(code=RunErrorCode.WORKER_FAILED, message=unsafe)


def test_client_idempotency_key_is_never_persisted_raw(tmp_path: Path) -> None:
    # Break caught: transient client-controlled traversal/credential/bidi material reaching DB.
    repository = _repository(tmp_path)
    hostile_key = "../../etc/passwd bearer TOPSECRET \u202egpj"
    record = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=hostile_key), now=T0
    ).record

    assert not hasattr(record, "client_idempotency_key")
    assert len(record.client_idempotency_key_digest) == 64
    assert hostile_key.encode("utf-8") not in repository.database_path.read_bytes()


def test_failure_contract_refuses_repr_bidi_path_and_colon_secret_payload() -> None:
    # Break caught: arbitrary exception reprs surviving weak regex-based sanitization.
    hostile = (
        "VendorResponse(account='U12345', client_secret: TOPSECRET, "
        "file='/Users/alice/vendor.json') \u202egpj"
    )
    with pytest.raises(ValueError):
        RunFailureV1(code=RunErrorCode.WORKER_FAILED, message=hostile)


@pytest.mark.parametrize(
    ("operation", "bad_version"),
    [
        ("claim", True),
        ("advance", 2.0),
        ("ack", 2.0),
        ("attach", 6.0),
        ("fail", True),
        ("complete", 1.0),
        ("publish", 7.0),
    ],
)
def test_every_expected_version_boundary_rejects_bool_and_float_before_mutation(
    tmp_path: Path, operation: str, bad_version: object
) -> None:
    # Break caught: Python True/1.0 equality bypassing optimistic CAS typing.
    repository = _repository(tmp_path)
    if operation in {"complete"}:
        record = repository.create_or_join(
            _new_run(book_id=None, client_idempotency_key=operation), now=T0
        ).record
    else:
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        record = repository.create_or_join(_new_run(), now=T1).record

    with pytest.raises((TypeError, ValueError)):
        if operation == "claim":
            repository.claim_start(
                record.run_id, expected_version=bad_version, now=T2
            )
        elif operation == "advance":
            record = repository.claim_start(
                record.run_id, expected_version=record.version, now=T2
            )
            repository.advance_stage(
                record.run_id,
                RunStage.RECONCILING,
                expected_version=bad_version,
                now=T3,
            )
        elif operation == "ack":
            record = repository.request_cancel(record.run_id, now=T2)
            repository.acknowledge_cancel(
                record.run_id, expected_version=bad_version, now=T3
            )
        elif operation == "attach":
            record = repository.claim_start(
                record.run_id, expected_version=record.version, now=T2
            )
            for stage in (
                RunStage.RECONCILING,
                RunStage.VALIDATING,
                RunStage.MODELING,
                RunStage.PUBLISHING,
            ):
                record = repository.advance_stage(
                    record.run_id,
                    stage,
                    expected_version=record.version,
                    now=T2,
                )
            repository.attach_candidate(
                record.run_id,
                SNAPSHOT_A,
                expected_version=bad_version,
                now=T3,
            )
        elif operation == "fail":
            repository.mark_failed(
                record.run_id,
                RunFailureV1(
                    code=RunErrorCode.WORKER_FAILED,
                ),
                expected_version=bad_version,
                now=T2,
            )
        elif operation == "complete":
            repository.complete_nonpublishing(
                record.run_id,
                '{"ok":true}',
                expected_version=bad_version,
                now=T2,
            )
        else:
            record = _publishing_run(repository)
            repository.commit_publication(
                record.run_id,
                _publication(SNAPSHOT_A, generation=1),
                expected_version=bad_version,
                now=T3,
            )


def test_nfc_equivalent_book_run_kind_and_client_key_share_stored_identity(
    tmp_path: Path,
) -> None:
    # Break caught: hashing NFC while partitioning SQLite uniqueness by raw NFD strings.
    repository = _repository(tmp_path)
    nfc_book = "caf\u00e9"
    nfd_book = unicodedata.normalize("NFD", nfc_book)
    first_head = repository.advance_book_head(nfc_book, 1, BOOK_REF_1, now=T0)
    second_head = repository.advance_book_head(nfd_book, 1, BOOK_REF_1, now=T1)
    assert second_head == first_head

    nfc_kind = "R\u00c9SUM\u00c9"
    nfd_kind = unicodedata.normalize("NFD", nfc_kind)
    nfc_key = "cl\u00e9"
    nfd_key = unicodedata.normalize("NFD", nfc_key)
    first = repository.create_or_join(
        NewRunV1(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6N1A",
            run_kind=nfc_kind,
            request_fingerprint="7" * 64,
            client_idempotency_key=nfc_key,
            book_id=None,
            target_cut_utc=None,
        ),
        now=T1,
    )
    joined = repository.create_or_join(
        NewRunV1(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6N1B",
            run_kind=nfd_kind,
            request_fingerprint="7" * 64,
            client_idempotency_key=nfd_key,
            book_id=None,
            target_cut_utc=None,
        ),
        now=T2,
    )
    assert joined.created is False
    assert joined.record.run_id == first.record.run_id
    assert joined.record.run_kind == nfc_kind


def test_terminal_rows_reject_every_later_mutation(tmp_path: Path) -> None:
    # Break caught: late callbacks changing immutable terminal evidence.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    terminal = repository.mark_failed(
        record.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=record.version,
        now=T2,
    )

    with pytest.raises(TerminalRunMutationError):
        repository.request_cancel(record.run_id, now=T3)
    with pytest.raises(TerminalRunMutationError):
        repository.claim_start(
            record.run_id, expected_version=terminal.version, now=T3
        )
    assert repository.get(record.run_id) == terminal


@pytest.mark.parametrize("stage", list(RunStage))
def test_startup_recovery_marks_every_running_stage_failed_interrupted(
    tmp_path: Path, stage: RunStage
) -> None:
    # Break caught: queued or later work surviving restart as fictitiously RUNNING.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    if stage is not RunStage.QUEUED:
        record = repository.claim_start(record.run_id, expected_version=record.version, now=T1)
        for next_stage in (
            RunStage.RECONCILING,
            RunStage.VALIDATING,
            RunStage.MODELING,
            RunStage.PUBLISHING,
        ):
            if record.run_stage is stage:
                break
            record = repository.advance_stage(
                record.run_id, next_stage, expected_version=record.version, now=T1
            )

    recovered_ids = RunRepository(tmp_path).recover_interrupted(now=T2)
    recovered = repository.get(record.run_id)

    assert recovered_ids == (record.run_id,)
    assert recovered.run_stage is stage
    assert recovered.run_outcome is RunOutcome.FAILED
    assert recovered.error_code is RunErrorCode.INTERRUPTED
    assert recovered.finished_at_utc == T2


@pytest.mark.parametrize(
    "outcome",
    [RunOutcome.SUCCEEDED, RunOutcome.FAILED, RunOutcome.CANCELLED],
)
def test_startup_recovery_leaves_terminal_rows_byte_for_byte_unchanged(
    tmp_path: Path, outcome: RunOutcome
) -> None:
    # Break caught: restart rewriting terminal timestamps, versions, results, or failures.
    repository = _repository(tmp_path)
    record = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=outcome.value), now=T0
    ).record
    if outcome is RunOutcome.SUCCEEDED:
        terminal = repository.complete_nonpublishing(
            record.run_id,
            adapt_legacy_result(True),
            expected_version=record.version,
            now=T1,
        )
    elif outcome is RunOutcome.FAILED:
        terminal = repository.mark_failed(
            record.run_id,
            RunFailureV1(code=RunErrorCode.WORKER_FAILED),
            expected_version=record.version,
            now=T1,
        )
    else:
        requested = repository.request_cancel(record.run_id, now=T1)
        terminal = repository.acknowledge_cancel(
            record.run_id, expected_version=requested.version, now=T2
        )

    assert repository.recover_interrupted(now=T3) == ()
    assert repository.get(record.run_id) == terminal


def test_repository_rejects_non_utc_timestamps_and_unknown_runs(tmp_path: Path) -> None:
    # Break caught: SQLite local-time ambiguity or silent mutation of a nonexistent run.
    repository = _repository(tmp_path)
    with pytest.raises(ValueError):
        repository.advance_book_head(
            "book-alpha",
            1,
            BOOK_REF_1,
            now=datetime(2026, 8, 20, 8, 0),
        )
    with pytest.raises(RunNotFoundError):
        repository.get("run_01J5X5S8J5J8P7KQ4Y0T3N6M9Z")


def test_public_lifecycle_mutations_reject_time_travel(tmp_path: Path) -> None:
    # Break caught: updated/finished timestamps preceding durable request/update history.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    record = repository.create_or_join(_new_run(), now=T2).record

    with pytest.raises(ValueError):
        repository.claim_start(
            record.run_id, expected_version=record.version, now=T1
        )
    with pytest.raises(ValueError):
        repository.mark_failed(
            record.run_id,
            RunFailureV1(
                code=RunErrorCode.WORKER_FAILED,
            ),
            expected_version=record.version,
            now=T1,
        )


def test_sql_rejects_unknown_error_invalid_time_schema_and_manifest_path(
    tmp_path: Path,
) -> None:
    # Break caught: a claimed-v1 catalog accepting values its typed reader cannot decode.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    record = _publishing_run(repository)
    repository.commit_publication(
        record.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=record.version,
        now=T3,
    )

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_runs SET error_code = 'TOTALLY_UNKNOWN' WHERE run_id = ?",
                (record.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_runs SET updated_at_utc = '2026-01-01' WHERE run_id = ?",
                (record.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET finished_at_utc = '2026-08-20T07:59:00.000000Z'
                WHERE run_id = ?
                """,
                (record.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_manifests SET schema_version = 'other_v1'
                WHERE snapshot_id = ?
                """,
                (SNAPSHOT_A,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_manifests SET manifest_relpath = '../../outside.json'
                WHERE snapshot_id = ?
                """,
                (SNAPSHOT_A,),
            )


def test_sql_rejects_finish_before_started_at(tmp_path: Path) -> None:
    # Break caught: 08:00 finish accepted after an 08:01 start when update is 08:02.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="finish-before-start"), now=T0
    ).record
    repository.claim_start(run.run_id, expected_version=run.version, now=T1)

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'FAILED', error_code = 'WORKER_FAILED',
                    error_message = 'worker execution failed',
                    finished_at_utc = '2026-08-20T08:00:00.000000Z',
                    updated_at_utc = '2026-08-20T08:02:00.000000Z'
                WHERE run_id = ?
                """,
                (run.run_id,),
            )


def test_sql_rejects_finish_before_cancel_intent(tmp_path: Path) -> None:
    # Break caught: cancellation completion predating its durable 08:01 intent.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="finish-before-cancel"), now=T0
    ).record
    repository.request_cancel(run.run_id, now=T1)

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'CANCELLED', error_code = 'CANCELLED_BY_USER',
                    error_message = 'cancelled by user',
                    finished_at_utc = '2026-08-20T08:00:00.000000Z',
                    updated_at_utc = '2026-08-20T08:02:00.000000Z'
                WHERE run_id = ?
                """,
                (run.run_id,),
            )


@pytest.mark.parametrize(
    ("column", "malformed"),
    [
        ("error_code", "TOTALLY_UNKNOWN"),
        ("updated_at_utc", "2026-01-01"),
    ],
)
def test_hostile_durable_run_rows_fail_as_typed_database_error(
    tmp_path: Path, column: str, malformed: str
) -> None:
    # Break caught: raw Enum/datetime/Pydantic ValueError escaping a corrupted catalog read.
    repository = _repository(tmp_path)
    record = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=column), now=T0
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE snapshot_runs SET {column} = ? WHERE run_id = ?",
            (malformed, record.run_id),
        )

    with pytest.raises(RunDatabaseError):
        repository.get(record.run_id)


def test_constraint_bypassed_65_character_run_kind_fails_typed(
    tmp_path: Path,
) -> None:
    # Break caught: decoded model accepting a run kind the v1 SQL contract caps at 64.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="long-run-kind"), now=T0
    ).record
    _execute_without_named_trigger(
        repository,
        "snapshot_run_allocation_immutable",
        "UPDATE snapshot_runs SET run_kind = ? WHERE run_id = ?",
        ("R" * 65, run.run_id),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


@pytest.mark.parametrize("table", ["book_heads", "snapshot_runs", "active_snapshots"])
def test_text_primary_keys_reject_null_at_the_sql_boundary(
    tmp_path: Path, table: str
) -> None:
    # Break caught: rowid-table TEXT PRIMARY KEY columns accepting NULL identifiers.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="null-primary-key"), now=T0
    ).record
    published_run = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6N1L",
        snapshot_id=SNAPSHOT_A,
    )
    repository.commit_publication(
        published_run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=published_run.version,
        now=T3,
    )
    target = {
        "book_heads": ("book_id", "book-alpha"),
        "snapshot_runs": ("run_id", run.run_id),
        "active_snapshots": ("book_id", "book-alpha"),
    }[table]

    with sqlite3.connect(repository.database_path) as connection:
        column, identity = target
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {table} SET {column} = NULL WHERE {column} = ?",
                (identity,),
            )


def test_hostile_durable_publication_and_result_rows_fail_typed(tmp_path: Path) -> None:
    # Break caught: direct catalog corruption escaping strict record invariants on read.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    publishing = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        publishing.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=publishing.version,
        now=T3,
    )
    result_run = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6R1A",
            book_id=None,
            client_idempotency_key="result-corruption",
        ),
        now=T0,
    ).record
    repository.complete_nonpublishing(
        result_run.run_id,
        adapt_legacy_result(1),
        expected_version=result_run.version,
        now=T1,
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        """
        UPDATE snapshot_manifests SET manifest_relpath = '../../outside.json'
        WHERE snapshot_id = ?
        """,
        (SNAPSHOT_A,),
        ignore_check_constraints=True,
    )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_UPDATE_BYPASS,
        """
        UPDATE snapshot_runs
        SET result_json = '{"api_key":"TOPSECRET","schema_version":"durable_run_result_v1"}'
        WHERE run_id = ?
        """,
        (result_run.run_id,),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.list_publications("book-alpha")
    with pytest.raises(RunDatabaseError):
        repository.get(result_run.run_id)


def test_hostile_durable_running_row_cannot_carry_a_valid_result(tmp_path: Path) -> None:
    # Break caught: constraint bypass leaving a typed result attached to a live run.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="live-result-corruption"), now=T0
    ).record
    result_json = (
        '{"artifact_digest":null,"boolean_value":null,"integer_value":1,'
        '"result_code":"INTEGER","schema_version":"durable_run_result_v1"}'
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET result_json = ? WHERE run_id = ?",
            (result_json, run.run_id),
        )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


@pytest.mark.parametrize(
    "corruption",
    [
        "running_with_finish",
        "failed_without_finish",
        "succeeded_with_error",
        "failed_without_error",
        "running_with_published_snapshot",
        "finish_before_start",
        "finish_before_cancel",
    ],
)
def test_constraint_bypassed_run_lifecycle_corruption_fails_typed(
    tmp_path: Path, corruption: str
) -> None:
    # Break caught: RunRecordV1 accepting state forbidden by the v1 SQL lifecycle contract.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=corruption), now=T0
    ).record
    if corruption == "finish_before_start":
        repository.claim_start(run.run_id, expected_version=run.version, now=T1)
    elif corruption == "finish_before_cancel":
        repository.request_cancel(run.run_id, now=T1)

    assignments = {
        "running_with_finish": (
            "finished_at_utc = '2026-08-20T08:01:00.000000Z', "
            "updated_at_utc = '2026-08-20T08:01:00.000000Z'"
        ),
        "failed_without_finish": (
            "run_outcome = 'FAILED', error_code = 'WORKER_FAILED', "
            "error_message = 'worker execution failed'"
        ),
        "succeeded_with_error": (
            "run_outcome = 'SUCCEEDED', error_code = 'WORKER_FAILED', "
            "error_message = 'worker execution failed', "
            "finished_at_utc = '2026-08-20T08:01:00.000000Z', "
            "updated_at_utc = '2026-08-20T08:01:00.000000Z'"
        ),
        "failed_without_error": (
            "run_outcome = 'FAILED', "
            "finished_at_utc = '2026-08-20T08:01:00.000000Z', "
            "updated_at_utc = '2026-08-20T08:01:00.000000Z'"
        ),
        "running_with_published_snapshot": f"published_snapshot_id = '{SNAPSHOT_A}'",
        "finish_before_start": (
            "run_outcome = 'FAILED', error_code = 'WORKER_FAILED', "
            "error_message = 'worker execution failed', "
            "finished_at_utc = '2026-08-20T08:00:00.000000Z', "
            "updated_at_utc = '2026-08-20T08:02:00.000000Z'"
        ),
        "finish_before_cancel": (
            "run_outcome = 'CANCELLED', error_code = 'CANCELLED_BY_USER', "
            "error_message = 'cancelled by user', "
            "finished_at_utc = '2026-08-20T08:00:00.000000Z', "
            "updated_at_utc = '2026-08-20T08:02:00.000000Z'"
        ),
    }[corruption]
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE snapshot_runs SET {assignments} WHERE run_id = ?",
            (run.run_id,),
        )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


@pytest.mark.parametrize(
    "corruption",
    [
        "book_only",
        "generation_only",
        "cut_only",
        "book_and_generation",
        "book_and_cut",
        "generation_and_cut",
        "negative_generation",
        "missing_expected_snapshot",
        "zero_expected_pointer",
    ],
)
def test_constraint_bypassed_run_identity_tuples_fail_typed(
    tmp_path: Path, corruption: str
) -> None:
    # Break caught: RunRecordV1 accepting tuple states forbidden by the v1 migration.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=corruption), now=T0
    ).record
    target_cut = "2026-08-20T08:00:00.000000Z"
    assignments = {
        "book_only": "book_id = 'book-alpha'",
        "generation_only": "captured_generation = 1",
        "cut_only": f"target_cut_utc = '{target_cut}'",
        "book_and_generation": "book_id = 'book-alpha', captured_generation = 1",
        "book_and_cut": (
            f"book_id = 'book-alpha', target_cut_utc = '{target_cut}'"
        ),
        "generation_and_cut": (
            f"captured_generation = 1, target_cut_utc = '{target_cut}'"
        ),
        "negative_generation": (
            "book_id = 'book-alpha', captured_generation = -1, "
            f"target_cut_utc = '{target_cut}'"
        ),
        "missing_expected_snapshot": "expected_active_pointer_version = 1",
        "zero_expected_pointer": (
            f"expected_active_snapshot_id = '{SNAPSHOT_A}', "
            "expected_active_pointer_version = 0"
        ),
    }[corruption]
    _execute_without_named_trigger(
        repository,
        "snapshot_run_allocation_immutable",
        f"UPDATE snapshot_runs SET {assignments} WHERE run_id = ?",
        (run.run_id,),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


def test_atomic_publication_commits_manifest_run_and_active_pointer_on_reopen(
    tmp_path: Path,
) -> None:
    # Break caught: a successful run, catalog row, and active pointer becoming visible in
    # different transactions or conflating the body snapshot ID with envelope bytes.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    reopened = RunRepository(tmp_path)
    reopened.initialize()
    durable_run = reopened.get(run.run_id)
    active = reopened.get_active("book-alpha")
    history = reopened.list_publications("book-alpha")
    assert result.published is True
    assert result.already_published is False
    assert durable_run.run_outcome is RunOutcome.SUCCEEDED
    assert durable_run.published_snapshot_id == SNAPSHOT_A
    assert active.snapshot_id == SNAPSHOT_A
    assert active.book_generation == 1
    assert active.pointer_version == 1
    assert len(history) == 1
    assert history[0].publication_sequence == 1
    assert history[0].snapshot_id == SNAPSHOT_A
    assert history[0].envelope_sha256 == "e" * 64
    assert history[0].envelope_sha256 != history[0].snapshot_id


def test_next_publication_captures_and_compares_expected_active_version(
    tmp_path: Path,
) -> None:
    # Break caught: generation-only CAS allowing a same-generation pointer overwrite.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T2,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )

    assert second.expected_active_snapshot_id == SNAPSHOT_A
    assert second.expected_active_pointer_version == 1
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T3,
    )
    active = repository.get_active("book-alpha")
    assert active.snapshot_id == SNAPSHOT_B
    assert active.pointer_version == 2


def test_list_active_returns_all_books_in_deterministic_nfc_order(
    tmp_path: Path,
) -> None:
    # Break caught: startup having no safe, deterministic repository enumeration seam.
    repository = _repository(tmp_path)
    assert repository.list_active() == ()
    nfc_book = "caf\u00e9"
    nfd_book = unicodedata.normalize("NFD", nfc_book)
    fixtures = (
        ("zeta", "run_01J5X5S8J5J8P7KQ4Y0T3N6L1A", SNAPSHOT_A),
        (nfd_book, "run_01J5X5S8J5J8P7KQ4Y0T3N6L1B", SNAPSHOT_B),
        ("alpha", "run_01J5X5S8J5J8P7KQ4Y0T3N6L1C", SNAPSHOT_C),
    )
    for book_id, run_id, snapshot_id in fixtures:
        repository.advance_book_head(book_id, 1, BOOK_REF_1, now=T0)
        run = _publishing_run(
            repository,
            run_id=run_id,
            snapshot_id=snapshot_id,
            book_id=book_id,
        )
        repository.commit_publication(
            run.run_id,
            _publication(snapshot_id, generation=1, book_id=book_id),
            expected_version=run.version,
            now=T3,
        )

    active = RunRepository(tmp_path).list_active()

    assert isinstance(active, tuple)
    assert [record.book_id for record in active] == ["alpha", nfc_book, "zeta"]
    assert [record.snapshot_id for record in active] == [
        SNAPSHOT_C,
        SNAPSHOT_B,
        SNAPSHOT_A,
    ]


def test_list_active_maps_corrupt_durable_rows_to_typed_database_error(
    tmp_path: Path,
) -> None:
    # Break caught: startup enumeration bypassing the strict ActiveSnapshotV1 decoder.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        "UPDATE active_snapshots SET updated_at_utc = '2026-08-20'",
        (),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.list_active()


@pytest.mark.parametrize(
    "tamper_sql",
    [
        (
            "UPDATE active_snapshots SET snapshot_id = ? "
            "WHERE book_id = 'book-alpha'"
        ),
        (
            "UPDATE active_snapshots SET book_generation = 2 "
            "WHERE book_id = 'book-alpha'"
        ),
    ],
)
def test_active_pointer_composite_binding_is_enforced_by_sqlite(
    tmp_path: Path, tamper_sql: str
) -> None:
    # Break caught: independent FKs allowing a foreign-book or wrong-generation pointer.
    repository = _repository(tmp_path)
    _publish_alpha_and_beta(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        parameters = (SNAPSHOT_BETA,) if "snapshot_id = ?" in tamper_sql else ()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(tamper_sql, parameters)


@pytest.mark.parametrize("tamper", ["foreign_book", "wrong_generation"])
@pytest.mark.parametrize("operation", ["initialize", "get", "list", "recover"])
def test_bypassed_active_binding_tamper_fails_every_read_boundary(
    tmp_path: Path, tamper: str, operation: str
) -> None:
    # Break caught: active reads trusting independently valid book/snapshot/generation fields.
    repository = _repository(tmp_path)
    _publish_alpha_and_beta(repository)
    if tamper == "foreign_book":
        statement = (
            "UPDATE active_snapshots SET snapshot_id = ? "
            "WHERE book_id = 'book-alpha'"
        )
        parameters = (SNAPSHOT_BETA,)
    else:
        statement = (
            "UPDATE active_snapshots SET book_generation = 2 "
            "WHERE book_id = 'book-alpha'"
        )
        parameters = ()
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        statement,
        parameters,
    )

    with pytest.raises(RunDatabaseError):
        if operation == "initialize":
            RunRepository(tmp_path).initialize()
        elif operation == "get":
            repository.get_active("book-alpha")
        elif operation == "list":
            repository.list_active()
        else:
            repository.recover_active("book-alpha", verify=_verified, now=T4)


@pytest.mark.parametrize("published_snapshot_id", [None, SNAPSHOT_B])
def test_sql_rejects_successful_book_run_without_its_candidate_publication_id(
    tmp_path: Path, published_snapshot_id: str | None
) -> None:
    # Break caught: a successful book run claiming no publication or a different one.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_runs SET published_snapshot_id = ? WHERE run_id = ?",
                (published_snapshot_id, run.run_id),
            )


@pytest.mark.parametrize("operation", ["initialize", "list", "idempotent_publish"])
def test_manifest_run_provenance_reassignment_fails_closed(
    tmp_path: Path, operation: str
) -> None:
    # Break caught: FK-clean reassignment of a publication to an unrelated successful run.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    published = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    publication = _publication(SNAPSHOT_A, generation=1)
    repository.commit_publication(
        published.run_id,
        publication,
        expected_version=published.version,
        now=T3,
    )
    spare = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6P1R",
            book_id=None,
            client_idempotency_key="provenance-spare",
        ),
        now=T0,
    ).record
    spare = repository.complete_nonpublishing(
        spare.run_id,
        adapt_legacy_result(None),
        expected_version=spare.version,
        now=T1,
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET run_id = ? WHERE snapshot_id = ?",
        (spare.run_id, SNAPSHOT_A),
        foreign_keys=True,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RunDatabaseError):
        if operation == "initialize":
            RunRepository(tmp_path).initialize()
        elif operation == "list":
            repository.list_publications("book-alpha")
        else:
            repository.commit_publication(
                published.run_id,
                publication,
                expected_version=published.version,
                now=T4,
            )


@pytest.mark.parametrize(
    "operation", ["initialize", "head", "run", "publications", "active"]
)
def test_lowered_head_generation_fails_every_relational_read_boundary(
    tmp_path: Path, operation: str
) -> None:
    # Break caught: a lower head generation making durable run/publication state impossible.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=2),
        expected_version=run.version,
        now=T3,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE book_heads SET generation = 1 WHERE book_id = 'book-alpha'"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RunDatabaseError):
        if operation == "initialize":
            RunRepository(tmp_path).initialize()
        elif operation == "head":
            repository.get_book_head("book-alpha")
        elif operation == "run":
            repository.get(run.run_id)
        elif operation == "publications":
            repository.list_publications("book-alpha")
        else:
            repository.get_active("book-alpha")


def test_sql_rejects_nonbook_run_with_expected_active_pointer(
    tmp_path: Path,
) -> None:
    # Break caught: a sync run carrying book-pointer state without an owning book.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    published = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        published.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=published.version,
        now=T3,
    )
    sync = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E1X",
            book_id=None,
            client_idempotency_key="expected-pointer-sync",
        ),
        now=T4,
    ).record

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET expected_active_snapshot_id = ?, expected_active_pointer_version = 1
                WHERE run_id = ?
                """,
                (SNAPSHOT_A, sync.run_id),
            )


def test_expected_active_snapshot_is_bound_to_same_book_by_sqlite(
    tmp_path: Path,
) -> None:
    # Break caught: an alpha run claiming beta's independently valid active publication.
    repository = _repository(tmp_path)
    _publish_alpha_and_beta(repository)
    run = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2X",
            client_idempotency_key="cross-book-expected",
        ),
        now=T4,
    ).record

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs SET expected_active_snapshot_id = ?
                WHERE run_id = ?
                """,
                (SNAPSHOT_BETA, run.run_id),
            )


@pytest.mark.parametrize("operation", ["initialize", "get"])
def test_expected_active_publication_cannot_postdate_its_run_request(
    tmp_path: Path, operation: str
) -> None:
    # Break caught: a run retroactively claiming a pointer published after allocation.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E3X",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        """
        UPDATE snapshot_runs SET expected_active_snapshot_id = ?
        WHERE run_id = ?
        """,
        (SNAPSHOT_B, second.run_id),
    )

    with pytest.raises(RunDatabaseError):
        if operation == "initialize":
            RunRepository(tmp_path).initialize()
        else:
            repository.get(second.run_id)


@pytest.mark.parametrize("tamper", ["manifest_time", "active_time"])
def test_cross_table_publication_timestamps_fail_closed(
    tmp_path: Path, tamper: str
) -> None:
    # Break caught: individually valid timestamps contradicting atomic publication order.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    if tamper == "manifest_time":
        _execute_without_named_triggers(
            repository,
            _MANIFEST_UPDATE_BYPASS,
            """
            UPDATE snapshot_manifests
            SET published_at_utc = '2026-08-20T08:03:01.000000Z'
            WHERE snapshot_id = ?
            """,
            (SNAPSHOT_A,),
        )
    else:
        _execute_without_named_triggers(
            repository,
            _ACTIVE_POINTER_UPDATE_BYPASS,
            """
            UPDATE active_snapshots
            SET updated_at_utc = '2026-08-20T08:02:59.000000Z'
            WHERE snapshot_id = ?
            """,
            (SNAPSHOT_A,),
        )

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


def test_sql_rejects_removed_recovery_event_with_selected_snapshot(
    tmp_path: Path,
) -> None:
    # Break caught: REMOVED evidence retaining a snapshot the pointer no longer selects.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    detail_json = '{"failures":[],"omitted_count":0}'

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json,
                    recorded_at_utc
                ) VALUES (
                    'book-alpha', ?, 1, 'REMOVED', ?, ?,
                    '2026-08-20T08:04:00.000000Z'
                )
                """,
                (SNAPSHOT_A, SNAPSHOT_A, detail_json),
            )


@pytest.mark.parametrize(
    ("action", "selected_snapshot_id"),
    [("REPOINTED", None), ("REMOVED", SNAPSHOT_A)],
)
def test_constraint_bypassed_recovery_event_coherence_fails_typed(
    tmp_path: Path, action: str, selected_snapshot_id: str | None
) -> None:
    # Break caught: RecoveryEventV1 not mirroring action/selection SQL coherence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _execute_without_named_triggers(
        repository,
        _RECOVERY_EVENT_INSERT_CAUSAL_BYPASS,
        """
        INSERT INTO snapshot_recovery_events (
            book_id, rejected_snapshot_id, expected_pointer_version,
            resolution_action, selected_snapshot_id, detail_json,
            recorded_at_utc
        ) VALUES (
            'book-alpha', ?, 1, ?, ?,
            '{"failures":[],"omitted_count":0}',
            '2026-08-20T08:04:00.000000Z'
        )
        """,
        (SNAPSHOT_A, action, selected_snapshot_id),
        ignore_check_constraints=True,
        foreign_keys=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")


def test_recovery_event_selected_snapshot_is_bound_to_same_book(
    tmp_path: Path,
) -> None:
    # Break caught: CAS evidence for alpha pointing at beta's publication.
    repository = _repository(tmp_path)
    _publish_alpha_and_beta(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json,
                    recorded_at_utc
                ) VALUES (
                    'book-alpha', ?, 1, 'CAS_LOST', ?,
                    '{"failures":[],"omitted_count":0}',
                    '2026-08-20T08:04:00.000000Z'
                )
                """,
                (SNAPSHOT_A, SNAPSHOT_BETA),
            )


def test_recovery_event_rejected_snapshot_is_bound_to_same_book(
    tmp_path: Path,
) -> None:
    # Break caught: alpha recovery evidence claiming beta's publication was rejected.
    repository = _repository(tmp_path)
    _publish_alpha_and_beta(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json,
                    recorded_at_utc
                ) VALUES (
                    'book-alpha', ?, 1, 'CAS_LOST', NULL,
                    '{"failures":[],"omitted_count":0}',
                    '2026-08-20T08:04:00.000000Z'
                )
                """,
                (SNAPSHOT_BETA,),
            )


def test_repointed_recovery_event_cannot_select_degraded_publication(
    tmp_path: Path,
) -> None:
    # Break caught: FK-clean audit evidence claiming a DEGRADED fallback was selected.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    degraded = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1G",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        degraded.run_id,
        _publication(SNAPSHOT_B, generation=1, status=SnapshotStatus.DEGRADED),
        expected_version=degraded.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json,
                    recorded_at_utc
                ) VALUES (
                    'book-alpha', ?, 2, 'REPOINTED', ?,
                    '{"failures":[],"omitted_count":0}',
                    '2026-08-20T08:05:00.000000Z'
                )
                """,
                (SNAPSHOT_A, SNAPSHOT_B),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sql_rejects_repointing_recovery_event_to_rejected_snapshot(
    tmp_path: Path,
) -> None:
    # Break caught: a REPOINTED event whose old and new pointer identities are equal.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshot_recovery_events (
                    book_id, rejected_snapshot_id, expected_pointer_version,
                    resolution_action, selected_snapshot_id, detail_json,
                    recorded_at_utc
                ) VALUES (
                    'book-alpha', ?, 1, 'REPOINTED', ?,
                    '{"failures":[],"omitted_count":0}',
                    '2026-08-20T08:04:00.000000Z'
                )
                """,
                (SNAPSHOT_A, SNAPSHOT_A),
            )


def test_same_generation_pointer_conflict_terminalizes_without_publication(
    tmp_path: Path,
) -> None:
    # Break caught: a valid generation check masking an active-pointer change made after
    # allocation, allowing stale same-generation analysis to become current.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    stale = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9C",
        snapshot_id=SNAPSHOT_C,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("active became corrupt")
        return _verified(snapshot_id)

    repository.recover_active("book-alpha", verify=verify, now=T5)
    rejected = repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=stale.version,
        now=T6,
    )

    assert rejected.published is False
    assert rejected.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert repository.get(stale.run_id).run_outcome is RunOutcome.FAILED
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert [row.snapshot_id for row in repository.list_publications("book-alpha")] == [
        SNAPSHOT_B,
        SNAPSHOT_A,
    ]


def test_stale_book_generation_terminalizes_without_publication(tmp_path: Path) -> None:
    # Break caught: older analytical work replacing a newer canonical book generation.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T2)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    assert result.published is False
    assert result.rejection_code is RunErrorCode.STALE_BOOK_GENERATION
    assert repository.get(run.run_id).error_code is RunErrorCode.STALE_BOOK_GENERATION
    assert repository.get_active("book-alpha") is None
    assert repository.list_publications("book-alpha") == ()


def test_stale_book_generation_uses_durable_clock_floor(
    tmp_path: Path,
) -> None:
    # Break caught: a publisher clock sampled before both the run's last update and a
    # newer book head moving terminal history backward while rejecting stale work.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T4)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T1,
    )

    assert result.published is False
    assert result.rejection_code is RunErrorCode.STALE_BOOK_GENERATION
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.STALE_BOOK_GENERATION
    assert result.run.finished_at_utc == T4
    assert result.run.updated_at_utc == T4
    assert repository.get_active("book-alpha") is None
    assert repository.list_publications("book-alpha") == ()


def test_cancel_at_publication_terminalizes_cancelled_without_manifest(
    tmp_path: Path,
) -> None:
    # Break caught: a finished candidate publishing after durable cancellation intent.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    requested = repository.request_cancel(run.run_id, now=T2)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=requested.version,
        now=T3,
    )

    assert result.published is False
    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert repository.get(run.run_id).run_outcome is RunOutcome.CANCELLED
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_cancel_intent_wins_publication_with_pre_cancel_expected_version(
    tmp_path: Path,
) -> None:
    # Break caught: stale-version rejection leaving a durably cancelled publisher RUNNING.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    repository.request_cancel(run.run_id, now=T2)

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert repository.list_publications("book-alpha") == ()


def test_cancel_intent_wins_when_publisher_clock_precedes_durable_cancel(
    tmp_path: Path,
) -> None:
    # Break caught: a T3 publisher clock sampled before cancellation committed at T4.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T2,
    )
    cancelled = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6C1K",
        snapshot_id=SNAPSHOT_B,
    )
    repository.request_cancel(cancelled.run_id, now=T4)

    result = repository.commit_publication(
        cancelled.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=cancelled.version,
        now=T3,
    )

    assert result.published is False
    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.cancel_requested_at_utc == T4
    assert result.run.finished_at_utc == T4
    assert result.run.updated_at_utc == T4
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert [row.snapshot_id for row in repository.list_publications("book-alpha")] == [
        SNAPSHOT_A
    ]

    reopened = RunRepository(tmp_path)
    assert reopened.get(cancelled.run_id) == result.run
    assert reopened.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert [row.snapshot_id for row in reopened.list_publications("book-alpha")] == [
        SNAPSHOT_A
    ]


def test_non_cancelled_publication_still_rejects_stale_caller_clock(
    tmp_path: Path,
) -> None:
    # Break caught: cancellation clock handling weakening ordinary publication monotonicity.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)

    with pytest.raises(RunDatabaseError):
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T1,
        )

    assert repository.get(run.run_id) == run
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_generic_failure_refuses_cancelled_by_user_code_and_sql_requires_intent(
    tmp_path: Path,
) -> None:
    # Break caught: impossible FAILED/CANCELLED_BY_USER rows without durable cancel intent.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    with pytest.raises(ValueError):
        repository.mark_failed(
            record.run_id,
            RunFailureV1(
                code=RunErrorCode.CANCELLED_BY_USER,
            ),
            expected_version=record.version,
            now=T2,
        )

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'CANCELLED', error_code = 'CANCELLED_BY_USER',
                    error_message = 'cancelled by user',
                    finished_at_utc = ?, updated_at_utc = ?
                WHERE run_id = ?
                """,
                (
                    T2.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                )
                * 2
                + (record.run_id,),
            )


def test_repeated_publication_is_idempotent_without_pointer_or_sequence_advance(
    tmp_path: Path,
) -> None:
    # Break caught: a retry after response loss inserting a second publication or pointer move.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    publication = _publication(SNAPSHOT_A, generation=1)
    first = repository.commit_publication(
        run.run_id, publication, expected_version=run.version, now=T2
    )
    repeated = repository.commit_publication(
        run.run_id, publication, expected_version=run.version, now=T3
    )

    assert first.published is True
    assert repeated.published is True
    assert repeated.already_published is True
    assert len(repository.list_publications("book-alpha")) == 1
    assert repository.get_active("book-alpha").pointer_version == 1


def test_concurrent_publication_returns_one_commit_and_one_idempotent_flag(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    publication = _publication(SNAPSHOT_A, generation=1)
    barrier = threading.Barrier(2)
    results: list[PublicationResultV1] = []
    errors: list[BaseException] = []

    def commit() -> None:
        try:
            barrier.wait()
            results.append(
                repository.commit_publication(
                    run.run_id,
                    publication,
                    expected_version=run.version,
                    now=T3,
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=commit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(result.already_published for result in results) == [False, True]
    assert all(result.published for result in results)
    assert len(repository.list_publications("book-alpha")) == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "db.after_manifest_insert",
        "db.after_run_update",
        "db.after_active_cas",
    ],
)
def test_precommit_fault_rolls_back_entire_publication_and_reopens_cleanly(
    tmp_path: Path, boundary: str
) -> None:
    # Break caught: any partial manifest/run/pointer state surviving a pre-commit fault.
    armed = False

    def fail(stage: str) -> None:
        if armed and stage == boundary:
            raise OSError(f"injected {stage}")

    repository = RunRepository(tmp_path, fault_injector=fail)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    armed = True

    with pytest.raises(RunDatabaseError):
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T3,
        )

    reopened = RunRepository(tmp_path)
    reopened.initialize()
    assert reopened.get(run.run_id) == run
    assert reopened.list_publications("book-alpha") == ()
    assert reopened.get_active("book-alpha") is None


def test_after_commit_fault_rereads_durable_success(tmp_path: Path) -> None:
    # Break caught: response uncertainty after commit being misreported/persisted as failure.
    armed = False

    def fail(stage: str) -> None:
        if armed and stage == "db.after_commit":
            raise OSError("lost response after durable commit")

    repository = RunRepository(tmp_path, fault_injector=fail)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    armed = True

    result = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    assert result.published is True
    assert RunRepository(tmp_path).get_active("book-alpha").snapshot_id == SNAPSHOT_A


def test_postcommit_result_reread_uses_one_consistent_snapshot(
    tmp_path: Path,
) -> None:
    # Break caught: a legitimate publisher advancing the pointer between result SELECTs
    # making an already-committed predecessor look like corrupt durable history.
    class RacingRepository(RunRepository):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.postcommit_armed = False
            self.interleaved = False

        def _inject(self, stage: str) -> None:
            super()._inject(stage)
            if stage == "db.after_commit":
                self.postcommit_armed = True

        def _pointer_row(
            self,
            connection: sqlite3.Connection,
            book_id: str,
        ) -> sqlite3.Row | None:
            row = RunRepository._pointer_row(connection, book_id)
            if self.postcommit_armed and not self.interleaved:
                self.interleaved = True
                competing = RunRepository(self.root)
                second = _publishing_run(
                    competing,
                    run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K1D",
                    snapshot_id=SNAPSHOT_B,
                )
                competing.commit_publication(
                    second.run_id,
                    _publication(SNAPSHOT_B, generation=1),
                    expected_version=second.version,
                    now=T4,
                )
            return row

    repository = RacingRepository(tmp_path)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)

    result = repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )

    reopened = RunRepository(tmp_path)
    assert repository.interleaved is True
    assert result.run.run_outcome is RunOutcome.SUCCEEDED
    assert result.active is not None
    assert result.active.snapshot_id == SNAPSHOT_A
    assert result.active.pointer_version == 1
    assert reopened.get(first.run_id).run_outcome is RunOutcome.SUCCEEDED
    durable_active = reopened.get_active("book-alpha")
    assert durable_active is not None
    assert durable_active.snapshot_id == SNAPSHOT_B
    assert durable_active.pointer_version == 2


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_precommit_process_control_exception_propagates_after_rollback(
    tmp_path: Path, signal_type: type[BaseException]
) -> None:
    # Break caught: outer publication handling wrapping process-control exceptions.
    signal = signal_type()
    armed = False

    def fail(stage: str) -> None:
        if armed and stage == "db.after_manifest_insert":
            raise signal

    repository = RunRepository(tmp_path, fault_injector=fail)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    armed = True

    with pytest.raises(signal_type) as captured:
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T3,
        )

    assert captured.value is signal
    reopened = RunRepository(tmp_path)
    assert reopened.get(run.run_id) == run
    assert reopened.list_publications("book-alpha") == ()
    assert reopened.get_active("book-alpha") is None


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_after_commit_process_control_exception_propagates_with_durable_success(
    tmp_path: Path, signal_type: type[BaseException]
) -> None:
    # Break caught: postcommit uncertainty handling swallowing process-control exceptions.
    signal = signal_type()
    armed = False

    def fail(stage: str) -> None:
        if armed and stage == "db.after_commit":
            raise signal

    repository = RunRepository(tmp_path, fault_injector=fail)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    armed = True

    with pytest.raises(signal_type) as captured:
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T3,
        )

    assert captured.value is signal
    reopened = RunRepository(tmp_path)
    assert reopened.get(run.run_id).run_outcome is RunOutcome.SUCCEEDED
    assert reopened.list_publications("book-alpha")[0].snapshot_id == SNAPSHOT_A
    assert reopened.get_active("book-alpha").snapshot_id == SNAPSHOT_A


def test_publication_preserves_existing_typed_repository_error(tmp_path: Path) -> None:
    # Break caught: catch-all wrapping RunDatabaseError inside another RunDatabaseError.
    marker = RunDatabaseError("typed marker")
    armed = False

    def fail(stage: str) -> None:
        if armed and stage == "db.after_manifest_insert":
            raise marker

    repository = RunRepository(tmp_path, fault_injector=fail)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository)
    armed = True

    with pytest.raises(RunDatabaseError) as captured:
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T3,
        )
    assert captured.value is marker


@pytest.mark.parametrize(
    "invalid_relpath",
    [
        "/tmp/manifest.json",
        "../manifest.json",
        "snapshots/manifests/analytical_snapshot_manifest_v1/aa/wrong.json",
    ],
)
def test_publication_metadata_rejects_unsafe_or_identity_mismatched_paths(
    invalid_relpath: str,
) -> None:
    # Break caught: absolute/traversal/arbitrary paths entering the durable catalog.
    with pytest.raises(ValueError):
        ManifestPublicationV1(
            **{
                **_publication(SNAPSHOT_A, generation=1).model_dump(mode="python"),
                "manifest_relpath": invalid_relpath,
            }
        )


def test_publication_refuses_candidate_or_book_metadata_mismatch(tmp_path: Path) -> None:
    # Break caught: catalog metadata claiming bytes/book/generation other than the run candidate.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    mismatched = _publication(SNAPSHOT_B, generation=1)

    with pytest.raises(PublicationConflictError):
        repository.commit_publication(
            run.run_id, mismatched, expected_version=run.version, now=T3
        )
    assert repository.get(run.run_id) == run


def test_blessed_fallback_history_uses_publication_sequence_not_time(
    tmp_path: Path,
) -> None:
    # Break caught: timestamp/filesystem ordering or DEGRADED rows entering fallback candidates.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    degraded = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        degraded.run_id,
        _publication(
            SNAPSHOT_B, generation=1, status=SnapshotStatus.DEGRADED
        ),
        expected_version=degraded.version,
        now=T3,
    )
    latest = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9C",
        snapshot_id=SNAPSHOT_C,
    )
    repository.commit_publication(
        latest.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=latest.version,
        now=T3,
    )

    fallbacks = repository.list_blessed_fallbacks(
        "book-alpha", excluding=SNAPSHOT_C
    )
    assert [record.snapshot_id for record in fallbacks] == [SNAPSHOT_A]
    assert [
        record.publication_sequence
        for record in repository.list_publications("book-alpha")
    ] == [3, 2, 1]


def test_corrupt_active_repoints_to_newest_verified_blessed_and_records_evidence(
    tmp_path: Path,
) -> None:
    # Break caught: recovery serving corrupt active bytes or mutating without audit evidence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T1)
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=2),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    recovered = repository.recover_active("book-alpha", verify=verify, now=T5)

    assert recovered.decision is ActiveRecoveryDecision.REPOINTED
    assert recovered.active.snapshot_id == SNAPSHOT_A
    assert recovered.active.pointer_version == 3
    events = repository.list_recovery_events("book-alpha")
    assert len(events) == 1
    assert events[0].rejected_snapshot_id == SNAPSHOT_B
    assert events[0].selected_snapshot_id == SNAPSHOT_A
    assert json.loads(events[0].detail_json) == {
        "failures": [
            {"error_code": "VERIFICATION_FAILED", "snapshot_id": SNAPSHOT_B}
        ],
        "omitted_count": 0,
    }


def test_no_verified_fallback_removes_active_pointer_and_records_decision(
    tmp_path: Path,
) -> None:
    # Break caught: corrupt active state remaining addressable when no safe candidate exists.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    recovered = repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )

    assert recovered.decision is ActiveRecoveryDecision.REMOVED
    assert recovered.active is None
    assert repository.get_active("book-alpha") is None
    assert repository.list_recovery_events("book-alpha")[0].selected_snapshot_id is None


def test_verified_active_is_unchanged_and_creates_no_recovery_event(tmp_path: Path) -> None:
    # Break caught: healthy active pointers being rewritten or creating false evidence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    recovered = repository.recover_active(
        "book-alpha", verify=_verified, now=T4
    )

    assert recovered.decision is ActiveRecoveryDecision.UNCHANGED
    assert recovered.active.pointer_version == 1
    assert repository.list_recovery_events("book-alpha") == ()


def test_verified_active_rereads_pointer_after_concurrent_publication(
    tmp_path: Path,
) -> None:
    # Break caught: healthy verification returning stale A/v1 after B/v2 was published.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6H1R",
        snapshot_id=SNAPSHOT_B,
    )
    published = False

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        nonlocal published
        if not published:
            repository.commit_publication(
                second.run_id,
                _publication(SNAPSHOT_B, generation=1),
                expected_version=second.version,
                now=T4,
            )
            published = True
        return _verified(snapshot_id)

    recovered = repository.recover_active("book-alpha", verify=verify, now=T5)

    assert recovered.decision is ActiveRecoveryDecision.CAS_LOST
    assert recovered.previous_active.snapshot_id == SNAPSHOT_A
    assert recovered.active.snapshot_id == SNAPSHOT_B
    assert recovered.active.pointer_version == 2
    assert recovered.event is None
    assert repository.list_recovery_events("book-alpha") == ()


def test_recovery_rejects_foreign_book_manifest_and_repoints_valid_fallback(
    tmp_path: Path,
) -> None:
    # Break caught: snapshot identity checks ignoring the manifest body's owning book.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    foreign = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6F1B",
        snapshot_id=SNAPSHOT_BETA,
    )
    repository.commit_publication(
        foreign.run_id,
        _publication(SNAPSHOT_BETA, generation=1),
        expected_version=foreign.version,
        now=T4,
    )

    recovered = repository.recover_active("book-alpha", verify=_verified, now=T5)

    assert recovered.decision is ActiveRecoveryDecision.REPOINTED
    assert recovered.active.snapshot_id == SNAPSHOT_A
    assert recovered.active.pointer_version == 3
    assert json.loads(recovered.event.detail_json)["failures"][0] == {
        "error_code": RecoveryRejectionCode.INVALID_VERIFIER_RESULT.value,
        "snapshot_id": SNAPSHOT_BETA,
    }


def test_recovery_rejects_wrong_generation_manifest_and_removes_active(
    tmp_path: Path,
) -> None:
    # Break caught: a generation-two manifest satisfying a generation-one catalog pointer.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_ALPHA_G2)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_ALPHA_G2, generation=1),
        expected_version=run.version,
        now=T3,
    )

    recovered = repository.recover_active("book-alpha", verify=_verified, now=T4)

    assert recovered.decision is ActiveRecoveryDecision.REMOVED
    assert recovered.active is None
    assert json.loads(recovered.event.detail_json)["failures"] == [
        {
            "error_code": RecoveryRejectionCode.INVALID_VERIFIER_RESULT.value,
            "snapshot_id": SNAPSHOT_ALPHA_G2,
        }
    ]


def test_recovery_rejects_catalog_and_manifest_status_mismatch(
    tmp_path: Path,
) -> None:
    # Break caught: a BLESSED manifest wrapper satisfying a DEGRADED catalog publication.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1, status=SnapshotStatus.DEGRADED),
        expected_version=run.version,
        now=T3,
    )

    recovered = repository.recover_active("book-alpha", verify=_verified, now=T4)

    assert recovered.decision is ActiveRecoveryDecision.REMOVED
    assert recovered.active is None
    assert json.loads(recovered.event.detail_json)["failures"] == [
        {
            "error_code": RecoveryRejectionCode.INVALID_VERIFIER_RESULT.value,
            "snapshot_id": SNAPSHOT_A,
        }
    ]


def test_recovery_repoint_aborts_if_selected_publication_metadata_changes(
    tmp_path: Path,
) -> None:
    # Break caught: verification selecting generation one, then repointing to tampered gen two.
    armed = False

    def mutate_selected_publication(stage: str) -> None:
        if armed and stage == "recovery.after_selection":
            _execute_without_named_triggers(
                repository,
                _MANIFEST_UPDATE_BYPASS,
                """
                UPDATE snapshot_manifests SET book_generation = 2
                WHERE snapshot_id = ?
                """,
                (SNAPSHOT_A,),
            )

    repository = RunRepository(tmp_path, fault_injector=mutate_selected_publication)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M2T",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    armed = True
    with pytest.raises(PublicationConflictError):
        repository.recover_active("book-alpha", verify=verify, now=T5)

    with sqlite3.connect(repository.database_path) as connection:
        active = connection.execute(
            "SELECT snapshot_id, book_generation FROM active_snapshots"
        ).fetchone()
        event_count = connection.execute(
            "SELECT count(*) FROM snapshot_recovery_events"
        ).fetchone()[0]
    assert active == (SNAPSHOT_B, 1)
    assert event_count == 0


def test_recovery_cas_detects_active_generation_change_after_selection(
    tmp_path: Path,
) -> None:
    # Break caught: recovery CAS matching only snapshot identity and pointer version.
    armed = False

    def mutate_active_generation(stage: str) -> None:
        if armed and stage == "recovery.after_selection":
            _execute_without_named_triggers(
                repository,
                _ACTIVE_POINTER_UPDATE_BYPASS,
                "UPDATE active_snapshots SET book_generation = 2",
                (),
            )

    repository = RunRepository(tmp_path, fault_injector=mutate_active_generation)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6C2S",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    armed = True
    with pytest.raises(RunDatabaseError):
        repository.recover_active("book-alpha", verify=verify, now=T5)

    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT snapshot_id, book_generation FROM active_snapshots"
        ).fetchone() == (SNAPSHOT_B, 2)
        assert connection.execute(
            "SELECT count(*) FROM snapshot_recovery_events"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("bypass", ["missing_manifest", "status_mismatch"])
def test_recovery_revalidates_verifier_output_and_embedded_manifest(
    tmp_path: Path, bypass: str
) -> None:
    # Break caught: isinstance accepting model_construct output or wrapper/manifest mismatch.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        if bypass == "missing_manifest":
            return VerifiedSnapshotV1.model_construct(
                snapshot_id=SNAPSHOT_A,
                status=SnapshotStatus.BLESSED,
                manifest=None,
            )
        return VerifiedSnapshotV1.model_construct(
            snapshot_id=SNAPSHOT_A,
            status=SnapshotStatus.DEGRADED,
            manifest=VERIFIED_A.manifest,
        )

    recovered = repository.recover_active(
        "book-alpha", verify=verify, now=T5
    )

    assert recovered.decision is ActiveRecoveryDecision.REMOVED
    assert recovered.active is None
    detail = json.loads(recovered.event.detail_json)
    assert detail["failures"][-1] == {
        "error_code": RecoveryRejectionCode.INVALID_VERIFIER_RESULT.value,
        "snapshot_id": SNAPSHOT_A,
    }


def test_recovery_does_not_swallow_process_control_exceptions(tmp_path: Path) -> None:
    # Break caught: a verifier KeyboardInterrupt being converted into durable rejection data.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    def interrupted(_snapshot_id: str) -> VerifiedSnapshotV1:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        repository.recover_active("book-alpha", verify=interrupted, now=T4)

    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert repository.list_recovery_events("book-alpha") == ()


def test_recovery_maps_hostile_dynamic_exception_name_to_closed_code(
    tmp_path: Path,
) -> None:
    # Break caught: arbitrary exception class names becoming durable error-code text.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    hostile_type = type(
        "authorization: bearer /Users/alice/secret\n\u202e",
        (Exception,),
        {},
    )

    recovered = repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(hostile_type("secret")),
        now=T4,
    )

    detail = json.loads(recovered.event.detail_json)
    assert detail == {
        "failures": [
            {
                "error_code": RecoveryRejectionCode.VERIFICATION_FAILED.value,
                "snapshot_id": SNAPSHOT_A,
            }
        ],
        "omitted_count": 0,
    }


def test_recovery_caps_700_candidate_failures_and_still_removes_active(
    tmp_path: Path,
) -> None:
    # Break caught: oversized evidence raising before corrupt-pointer removal/audit commit.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    timestamp = T0.isoformat(timespec="microseconds").replace("+00:00", "Z")
    snapshot_ids = [f"{index + 1_000:064x}" for index in range(701)]
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index, snapshot_id in enumerate(snapshot_ids):
            run_id = f"run_seed_{index:08d}"
            request_fingerprint = f"{index + 1:064x}"
            identity = RunRepository._idempotency_identity(
                _new_run(
                    run_id=run_id,
                    request_fingerprint=request_fingerprint,
                    client_idempotency_key=None,
                ),
                1,
                None,
            )
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
                    ?, 'ANALYTICAL_SNAPSHOT', ?, ?, NULL, 'book-alpha', 1,
                    ?, ?, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                    NULL, ?, ?, NULL, NULL, NULL, 1
                )
                """,
                (
                    run_id,
                    identity,
                    request_fingerprint,
                    None if index == 0 else snapshot_ids[index - 1],
                    index,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    snapshot_id,
                    snapshot_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO snapshot_manifests (
                    snapshot_id, run_id, book_id, book_generation, snapshot_status,
                    schema_version, hash_algorithm, manifest_relpath, envelope_sha256,
                    envelope_byte_length, published_at_utc
                ) VALUES (?, ?, 'book-alpha', 1, 'BLESSED',
                    'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 1, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{snapshot_id[:2]}/{snapshot_id}.json",
                    f"{index + 10_000:064x}",
                    timestamp,
                ),
            )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = ?, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (snapshot_ids[-1], len(snapshot_ids), timestamp),
        foreign_keys=True,
    )

    recovered = repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T1,
    )

    assert recovered.decision is ActiveRecoveryDecision.REMOVED
    assert repository.get_active("book-alpha") is None
    detail = json.loads(recovered.event.detail_json)
    assert len(detail["failures"]) == 128
    assert detail["omitted_count"] == 573
    assert len(recovered.event.detail_json.encode("utf-8")) < 65_536


def test_concurrent_recovery_cas_loser_cannot_overwrite_and_is_audited(
    tmp_path: Path,
) -> None:
    # Break caught: two startup verifiers both mutating from a stale active-pointer read.
    barrier = threading.Barrier(2)

    def synchronize(stage: str) -> None:
        if stage == "recovery.after_selection":
            barrier.wait()

    repository = RunRepository(tmp_path, fault_injector=synchronize)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T1)
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6M9B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=2),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt")
        return _verified(snapshot_id)

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                repository.recover_active("book-alpha", verify=verify, now=T5)
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert {result.decision for result in results} == {
        ActiveRecoveryDecision.REPOINTED,
        ActiveRecoveryDecision.CAS_LOST,
    }
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert repository.get_active("book-alpha").pointer_version == 3
    assert [event.resolution_action for event in repository.list_recovery_events("book-alpha")] == [
        ActiveRecoveryDecision.REPOINTED,
        ActiveRecoveryDecision.CAS_LOST,
    ]


def test_verified_active_rejects_concurrent_publication_metadata_change(
    tmp_path: Path,
) -> None:
    # Break caught: returning UNCHANGED after verifying metadata that no longer exists.
    # Policy: metadata drift fails once with a typed conflict; recovery does not retry I/O.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    changed = False

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        nonlocal changed
        if not changed:
            _execute_without_named_triggers(
                repository,
                _MANIFEST_UPDATE_BYPASS,
                """
                UPDATE snapshot_manifests SET envelope_sha256 = ?
                WHERE snapshot_id = ?
                """,
                ("d" * 64, snapshot_id),
            )
            changed = True
        return _verified(snapshot_id)

    with pytest.raises(PublicationConflictError):
        repository.recover_active("book-alpha", verify=verify, now=T4)

    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A
    assert repository.list_recovery_events("book-alpha") == ()


def test_nonbook_run_refuses_candidate_attachment_before_mutation(
    tmp_path: Path,
) -> None:
    # Break caught: a non-book run reaching PUBLISHING and gaining snapshot identity.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="nonbook-candidate"), now=T0
    ).record
    run = repository.claim_start(run.run_id, expected_version=run.version, now=T1)
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        run = repository.advance_stage(
            run.run_id, stage, expected_version=run.version, now=T2
        )

    with pytest.raises(IllegalRunTransitionError):
        repository.attach_candidate(
            run.run_id,
            SNAPSHOT_A,
            expected_version=run.version,
            now=T3,
        )

    assert repository.get(run.run_id).candidate_snapshot_id is None
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET candidate_snapshot_id = ? WHERE run_id = ?",
            (SNAPSHOT_A, run.run_id),
        )
        before = connection.execute(
            "SELECT candidate_snapshot_id, version FROM snapshot_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()

    with pytest.raises(RunDatabaseError):
        repository.attach_candidate(
            run.run_id,
            SNAPSHOT_A,
            expected_version=run.version,
            now=T3,
        )

    with sqlite3.connect(repository.database_path) as connection:
        after = connection.execute(
            "SELECT candidate_snapshot_id, version FROM snapshot_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert after == before == (SNAPSHOT_A, run.version)


def test_sql_and_model_reject_nonbook_candidate_snapshot_identity(
    tmp_path: Path,
) -> None:
    # Break caught: SQL and decoded state disagreeing about non-book candidate identity.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="nonbook-sql-candidate"),
        now=T0,
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_runs SET candidate_snapshot_id = ? WHERE run_id = ?",
                (SNAPSHOT_A, run.run_id),
            )
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET candidate_snapshot_id = ? WHERE run_id = ?",
            (SNAPSHOT_A, run.run_id),
        )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


def test_sql_and_model_reject_nonbook_published_snapshot_identity(
    tmp_path: Path,
) -> None:
    # Break caught: successful non-book work claiming a snapshot publication identity.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="nonbook-sql-published"),
        now=T0,
    ).record
    run = repository.complete_nonpublishing(
        run.run_id,
        adapt_legacy_result(None),
        expected_version=run.version,
        now=T1,
    )
    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_runs SET published_snapshot_id = ? WHERE run_id = ?",
                (SNAPSHOT_A, run.run_id),
            )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_UPDATE_BYPASS,
        "UPDATE snapshot_runs SET published_snapshot_id = ? WHERE run_id = ?",
        (SNAPSHOT_A, run.run_id),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)


def test_cas_lost_model_rejects_rejected_snapshot_as_selection() -> None:
    # Break caught: CAS_LOST evidence claiming its rejected identity was selected.
    with pytest.raises(ValueError):
        RecoveryEventV1(
            event_sequence=1,
            book_id="book-alpha",
            rejected_snapshot_id=SNAPSHOT_A,
            expected_pointer_version=1,
            resolution_action=ActiveRecoveryDecision.CAS_LOST,
            selected_snapshot_id=SNAPSHOT_A,
            detail_json='{"failures":[],"omitted_count":0}',
            recorded_at_utc=T4,
        )


def test_sql_rejects_nonnull_cas_lost_selection_that_is_not_safe(
    tmp_path: Path,
) -> None:
    # Break caught: CAS_LOST naming the rejected ID or a DEGRADED same-book publication.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    degraded = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6C4L",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        degraded.run_id,
        _publication(SNAPSHOT_B, generation=1, status=SnapshotStatus.DEGRADED),
        expected_version=degraded.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for selected_snapshot_id in (SNAPSHOT_A, SNAPSHOT_B):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO snapshot_recovery_events (
                        book_id, rejected_snapshot_id, expected_pointer_version,
                        resolution_action, selected_snapshot_id, detail_json,
                        recorded_at_utc
                    ) VALUES (
                        'book-alpha', ?, 2, 'CAS_LOST', ?,
                        '{"failures":[],"omitted_count":0}',
                        '2026-08-20T08:05:00.000000Z'
                    )
                    """,
                    (SNAPSHOT_A, selected_snapshot_id),
                )


def test_sql_accepts_cas_lost_null_selection_for_attempted_removal(
    tmp_path: Path,
) -> None:
    # Break caught: hardening non-null CAS evidence accidentally forbidding removal races.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', NULL,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:04:00.000000Z'
            )
            """,
            (SNAPSHOT_A,),
        )

    event = repository.list_recovery_events("book-alpha")[0]
    assert event.resolution_action is ActiveRecoveryDecision.CAS_LOST
    assert event.selected_snapshot_id is None


def test_sql_accepts_distinct_blessed_cas_lost_selection(
    tmp_path: Path,
) -> None:
    # Break caught: CAS hardening accidentally forbidding a safe attempted fallback.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6C6L",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', ?,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A, SNAPSHOT_B),
        )

    event = repository.list_recovery_events("book-alpha")[0]
    assert event.selected_snapshot_id == SNAPSHOT_B


def test_cas_lost_selected_publication_must_remain_blessed_on_read_and_audit(
    tmp_path: Path,
) -> None:
    # Break caught: durable CAS evidence continuing to trust a now-DEGRADED selection.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6C5L",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', ?,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A, SNAPSHOT_B),
        )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        """
        UPDATE snapshot_manifests SET snapshot_status = 'DEGRADED'
        WHERE snapshot_id = ?
        """,
        (SNAPSHOT_B,),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")
    with pytest.raises(RunDatabaseError):
        repository.audit_integrity()


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("book_heads", "version = 0"),
        ("snapshot_runs", "run_stage = 'UNKNOWN_STAGE'"),
        ("snapshot_runs", "run_outcome = 'UNKNOWN_OUTCOME'"),
        ("snapshot_runs", "error_code = 'UNKNOWN_ERROR'"),
        ("snapshot_manifests", "snapshot_status = 'UNKNOWN_STATUS'"),
        ("active_snapshots", "pointer_version = 0"),
        ("snapshot_recovery_events", "resolution_action = 'UNKNOWN_ACTION'"),
        (
            "snapshot_runs",
            "finished_at_utc = '2026-08-20T08:01:00.000000Z'",
        ),
        ("snapshot_runs", "run_outcome = 'FAILED'"),
    ],
)
def test_manual_and_initialize_audits_cover_scalar_and_lifecycle_domains(
    tmp_path: Path, table: str, assignment: str
) -> None:
    # Break caught: CHECK-bypassed closed-domain or lifecycle corruption escaping full audit.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    publication_run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        publication_run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=publication_run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    running = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6A6D",
            book_id=None,
            client_idempotency_key="audit-domain",
        ),
        now=T0,
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', NULL,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:04:00.000000Z'
            )
            """,
            (SNAPSHOT_A,),
        )
    where = {
        "book_heads": "book_id = 'book-alpha'",
        "snapshot_runs": f"run_id = '{running.run_id}'",
        "snapshot_manifests": f"snapshot_id = '{SNAPSHOT_A}'",
        "active_snapshots": "book_id = 'book-alpha'",
        "snapshot_recovery_events": "event_sequence = 1",
    }[table]
    bypass = {
        "snapshot_manifests": _MANIFEST_UPDATE_BYPASS,
        "active_snapshots": _ACTIVE_POINTER_UPDATE_BYPASS,
        "snapshot_recovery_events": _RECOVERY_EVENT_UPDATE_BYPASS,
    }.get(table)
    if bypass is None:
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(f"UPDATE {table} SET {assignment} WHERE {where}")
    else:
        _execute_without_named_triggers(
            repository,
            bypass,
            f"UPDATE {table} SET {assignment} WHERE {where}",
            (),
            ignore_check_constraints=True,
        )

    with pytest.raises(RunDatabaseError):
        repository.audit_integrity()
    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


@pytest.mark.parametrize("column", ["run_stage", "run_outcome"])
def test_recover_interrupted_fails_atomically_on_closed_domain_corruption(
    tmp_path: Path, column: str
) -> None:
    # Break caught: startup recovery skipping unknown outcomes or preserving unknown stages.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=f"bad-{column}"), now=T0
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE snapshot_runs SET {column} = ? WHERE run_id = ?",
            (f"UNKNOWN_{column.upper()}", run.run_id),
        )

    with pytest.raises(RunDatabaseError):
        repository.recover_interrupted(now=T2)

    with sqlite3.connect(repository.database_path) as connection:
        stored = connection.execute(
            f"SELECT {column}, finished_at_utc FROM snapshot_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert stored == (f"UNKNOWN_{column.upper()}", None)


def test_bound_publication_scalar_corruption_fails_typed_active_read(
    tmp_path: Path,
) -> None:
    # Break caught: active decoding ignoring the bound publication's closed status domain.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET snapshot_status = 'UNKNOWN_STATUS'",
        (),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.get_active("book-alpha")


def test_single_row_hot_paths_do_not_scale_with_100k_history(
    tmp_path: Path,
) -> None:
    # Break caught: reintroducing whole-catalog scans on get/claim transaction boundaries.
    small = _VmBudgetRunRepository(tmp_path / "small")
    large = _VmBudgetRunRepository(tmp_path / "large")
    small.initialize()
    large.initialize()
    small_target = small.create_or_join(
        _new_run(book_id=None, client_idempotency_key="small-vm-target"), now=T0
    ).record
    large_target = large.create_or_join(
        _new_run(book_id=None, client_idempotency_key="large-vm-target"), now=T0
    ).record
    _seed_nonbook_history(small, 100)
    _seed_nonbook_history(large, 100_000)

    def vm_costs(
        repository: _VmBudgetRunRepository, run_id: str
    ) -> tuple[int, int]:
        repository.reset_vm_callbacks()
        repository.get(run_id)
        get_callbacks = repository.vm_callbacks
        repository.reset_vm_callbacks()
        repository.claim_start(run_id, expected_version=1, now=T1)
        write_callbacks = repository.vm_callbacks
        return get_callbacks, write_callbacks

    small_get, small_write = vm_costs(small, small_target.run_id)
    large_get, large_write = vm_costs(large, large_target.run_id)

    assert large_get <= small_get + 20
    assert large_write <= small_write + 40


@pytest.mark.parametrize(
    ("case", "assignment"),
    [
        ("terminal_unknown_stage", "run_stage = 'UNKNOWN_STAGE'"),
        ("terminal_unknown_error", "error_code = 'UNKNOWN_ERROR'"),
        ("terminal_missing_finish", "finished_at_utc = NULL"),
        ("running_invalid_version", "version = 0"),
    ],
)
def test_running_mutations_decode_complete_target_before_business_errors(
    tmp_path: Path,
    case: str,
    assignment: str,
) -> None:
    # Break caught: terminal/version checks masking constraint-bypassed row corruption.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=case), now=T0
    ).record
    if case.startswith("terminal_"):
        run = repository.complete_nonpublishing(
            run.run_id,
            adapt_legacy_result(None),
            expected_version=run.version,
            now=T1,
        )
    if case.startswith("terminal_"):
        _execute_without_named_triggers(
            repository,
            _TERMINAL_RUN_UPDATE_BYPASS,
            f"UPDATE snapshot_runs SET {assignment} WHERE run_id = ?",
            (run.run_id,),
            ignore_check_constraints=True,
        )
    else:
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                f"UPDATE snapshot_runs SET {assignment} WHERE run_id = ?",
                (run.run_id,),
            )

    with pytest.raises(RunDatabaseError):
        repository.claim_start(run.run_id, expected_version=run.version, now=T2)


def test_running_mutation_validates_relations_before_terminal_error(
    tmp_path: Path,
) -> None:
    # Break caught: terminal classification masking an invalid owned-publication tuple.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    run = repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    ).run
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET book_generation = 2 WHERE snapshot_id = ?",
        (SNAPSHOT_A,),
    )

    with pytest.raises(RunDatabaseError):
        repository.claim_start(run.run_id, expected_version=run.version, now=T4)


def test_create_or_join_decodes_exact_identity_before_outcome_filter(
    tmp_path: Path,
) -> None:
    # Break caught: an unknown outcome hiding an exact identity and permitting a duplicate.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    request = _new_run(client_idempotency_key="corrupt-exact-identity")
    first = repository.create_or_join(request, now=T1).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET run_outcome = 'UNKNOWN_OUTCOME' WHERE run_id = ?",
            (first.run_id,),
        )

    with pytest.raises(RunDatabaseError):
        repository.create_or_join(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1A",
                client_idempotency_key="corrupt-exact-identity",
            ),
            now=T2,
        )

    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_runs").fetchone()[0] == 1


def test_create_or_join_decodes_book_generation_before_live_filter(
    tmp_path: Path,
) -> None:
    # Break caught: an unknown outcome hiding in the same book/generation neighborhood.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = repository.create_or_join(
        _new_run(client_idempotency_key="corrupt-book-generation"), now=T1
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET run_outcome = 'UNKNOWN_OUTCOME' WHERE run_id = ?",
            (first.run_id,),
        )

    with pytest.raises(RunDatabaseError):
        repository.create_or_join(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1B",
                request_fingerprint="b" * 64,
                client_idempotency_key="different-neighborhood-identity",
            ),
            now=T2,
        )

    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_runs").fetchone()[0] == 1


def test_list_blessed_fallbacks_decodes_unknown_status_before_filter(
    tmp_path: Path,
) -> None:
    # Break caught: a valid-value predicate silently omitting a corrupt same-book manifest.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1C",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET snapshot_status = 'UNKNOWN_STATUS' "
        "WHERE snapshot_id = ?",
        (SNAPSHOT_A,),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.list_blessed_fallbacks("book-alpha", excluding=SNAPSHOT_B)


def test_recovery_decodes_unknown_fallback_status_before_selection(
    tmp_path: Path,
) -> None:
    # Break caught: recovery removing a pointer because SQL hid a corrupt fallback row.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1D",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET snapshot_status = 'UNKNOWN_STATUS' "
        "WHERE snapshot_id = ?",
        (SNAPSHOT_A,),
        ignore_check_constraints=True,
    )

    with pytest.raises(RunDatabaseError):
        repository.recover_active(
            "book-alpha",
            verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
            now=T5,
        )

    with sqlite3.connect(repository.database_path) as connection:
        active_snapshot_id = connection.execute(
            "SELECT snapshot_id FROM active_snapshots WHERE book_id = 'book-alpha'"
        ).fetchone()[0]
        recovery_event_count = connection.execute(
            "SELECT count(*) FROM snapshot_recovery_events"
        ).fetchone()[0]
    assert active_snapshot_id == SNAPSHOT_B
    assert recovery_event_count == 0


def test_expected_active_reference_validates_publication_owner_provenance(
    tmp_path: Path,
) -> None:
    # Break caught: a dependent run trusting a manifest reassigned to a failed non-book owner.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    published = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        published.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=published.version,
        now=T3,
    )
    dependent = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1E",
            client_idempotency_key="dependent-owner-provenance",
        ),
        now=T4,
    ).record
    invalid_owner = _failed_nonbook_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D1F",
        client_idempotency_key="invalid-expected-owner",
    )
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET run_id = ? WHERE snapshot_id = ?",
        (invalid_owner.run_id, SNAPSHOT_A),
        foreign_keys=True,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RunDatabaseError):
        repository.get(dependent.run_id)


@pytest.mark.parametrize("reference", ["rejected", "selected"])
def test_recovery_event_references_validate_publication_owner_provenance(
    tmp_path: Path,
    reference: str,
) -> None:
    # Break caught: recovery evidence trusting a referenced manifest with an invalid owner.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D2A",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', ?,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A, SNAPSHOT_B),
        )
    invalid_owner = _failed_nonbook_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D2B",
        client_idempotency_key=f"invalid-event-{reference}-owner",
    )
    target_snapshot_id = SNAPSHOT_A if reference == "rejected" else SNAPSHOT_B
    _execute_without_named_triggers(
        repository,
        _MANIFEST_UPDATE_BYPASS,
        "UPDATE snapshot_manifests SET run_id = ? WHERE snapshot_id = ?",
        (invalid_owner.run_id, target_snapshot_id),
        foreign_keys=True,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")


def test_recovery_repoint_rejects_changed_rejected_publication_metadata(
    tmp_path: Path,
) -> None:
    # Break caught: recording a repoint after the rejected publication tuple drifted.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D2C",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            _execute_without_named_triggers(
                repository,
                _MANIFEST_UPDATE_BYPASS,
                "UPDATE snapshot_manifests SET envelope_sha256 = ? "
                "WHERE snapshot_id = ?",
                ("d" * 64, SNAPSHOT_B),
            )
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    with pytest.raises(PublicationConflictError):
        repository.recover_active("book-alpha", verify=verify, now=T5)

    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_B
    assert repository.list_recovery_events("book-alpha") == ()


def test_all_publication_status_updates_are_rejected_after_insert(
    tmp_path: Path,
) -> None:
    # Break caught: treating only selected recovery publications as immutable.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6D2D",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', ?,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A, SNAPSHOT_B),
        )
        for snapshot_id, status in (
            (SNAPSHOT_A, "DEGRADED"),
            (SNAPSHOT_A, "BLESSED"),
            (SNAPSHOT_B, "DEGRADED"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE snapshot_manifests SET snapshot_status = ? "
                    "WHERE snapshot_id = ?",
                    (status, snapshot_id),
                )
        statuses = dict(
            connection.execute(
                "SELECT snapshot_id, snapshot_status FROM snapshot_manifests"
            ).fetchall()
        )

    assert statuses == {SNAPSHOT_A: "BLESSED", SNAPSHOT_B: "BLESSED"}


def test_head_hot_paths_use_generation_indexes_with_100k_same_book_rows(
    tmp_path: Path,
) -> None:
    # Break caught: head validation scanning all same-book run history.
    small = _VmBudgetRunRepository(tmp_path / "small-head")
    large = _VmBudgetRunRepository(tmp_path / "large-head")
    for repository, count in ((small, 100), (large, 100_000)):
        repository.initialize()
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        _seed_same_book_terminal_history(repository, count)

    def head_vm_costs(repository: _VmBudgetRunRepository) -> tuple[int, int]:
        repository.reset_vm_callbacks()
        repository.get_book_head("book-alpha")
        read_callbacks = repository.vm_callbacks
        repository.reset_vm_callbacks()
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T1)
        write_callbacks = repository.vm_callbacks
        return read_callbacks, write_callbacks

    small_read, small_write = head_vm_costs(small)
    large_read, large_write = head_vm_costs(large)
    assert large_read <= small_read + 20
    assert large_write <= small_write + 40

    with sqlite3.connect(large.database_path) as connection:
        run_plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM snapshot_runs "
                "WHERE book_id = ? AND captured_generation > ? LIMIT 1",
                ("book-alpha", 1),
            )
        )
        manifest_plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM snapshot_manifests "
                "WHERE book_id = ? AND book_generation > ? LIMIT 1",
                ("book-alpha", 1),
            )
        )
    assert "snapshot_runs_by_book_generation" in run_plan
    assert "snapshot_manifests_by_book_generation" in manifest_plan


def test_recovery_event_listing_uses_index_with_100k_unrelated_rows(
    tmp_path: Path,
) -> None:
    # Break caught: one-book evidence reads scanning the global event history.
    small = _VmBudgetRunRepository(tmp_path / "small-events")
    large = _VmBudgetRunRepository(tmp_path / "large-events")
    for repository, count in ((small, 100), (large, 100_000)):
        repository.initialize()
        _publish_alpha_and_beta(repository)
        _seed_recovery_event_history(
            repository,
            1,
            book_id="book-alpha",
            rejected_snapshot_id=SNAPSHOT_A,
        )
        _seed_recovery_event_history(
            repository,
            count,
            book_id="book-beta",
            rejected_snapshot_id=SNAPSHOT_BETA,
        )

    def list_vm_cost(repository: _VmBudgetRunRepository) -> int:
        repository.reset_vm_callbacks()
        events = repository.list_recovery_events("book-alpha")
        assert len(events) == 1
        return repository.vm_callbacks

    small_cost = list_vm_cost(small)
    large_cost = list_vm_cost(large)
    assert large_cost <= small_cost + 40

    with sqlite3.connect(large.database_path) as connection:
        plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM snapshot_recovery_events "
                "WHERE book_id = ? ORDER BY event_sequence",
                ("book-alpha",),
            )
        )
    assert "recovery_events_by_book_sequence" in plan


def test_recovery_event_append_is_constant_with_100k_same_book_rows(
    tmp_path: Path,
) -> None:
    # Break caught: causal-clock enforcement scanning all prior same-book events.
    small = _VmBudgetRunRepository(tmp_path / "small-event-append")
    large = _VmBudgetRunRepository(tmp_path / "large-event-append")
    for repository, count in ((small, 100), (large, 100_000)):
        repository.initialize()
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
        repository.commit_publication(
            run.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=run.version,
            now=T3,
        )
        _seed_recovery_event_history(
            repository,
            count,
            book_id="book-alpha",
            rejected_snapshot_id=SNAPSHOT_A,
        )

    def pointer_read_cost(repository: _VmBudgetRunRepository) -> int:
        repository.reset_vm_callbacks()
        assert repository.get_active("book-alpha") is None
        return repository.vm_callbacks

    def append_vm_cost(repository: _VmBudgetRunRepository) -> int:
        repository.reset_vm_callbacks()
        with repository._write_transaction() as connection:
            _insert_raw_recovery_event(
                connection,
                rejected_snapshot_id=SNAPSHOT_A,
                expected_pointer_version=1,
                recorded_at_utc="2026-08-20T08:05:00.000000Z",
            )
        return repository.vm_callbacks

    small_read = pointer_read_cost(small)
    large_read = pointer_read_cost(large)
    small_cost = append_vm_cost(small)
    large_cost = append_vm_cost(large)
    assert large_read <= small_read + 20
    assert large_cost <= small_cost + 20

    with sqlite3.connect(large.database_path) as connection:
        plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT recorded_at_utc "
                "FROM snapshot_recovery_events WHERE book_id = ? "
                "ORDER BY event_sequence DESC LIMIT 1",
                ("book-alpha",),
            )
        )
    assert "recovery_events_by_book_sequence" in plan
    with sqlite3.connect(large.database_path) as connection:
        transition_plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM snapshot_recovery_events "
                "WHERE book_id = ? AND expected_pointer_version = ? "
                "AND resolution_action IN ('REPOINTED', 'REMOVED')",
                ("book-alpha", 1),
            )
        )
    assert "recovery_events_by_book_pointer_version" in transition_plan


def test_complete_neighborhood_queries_use_named_full_indexes(tmp_path: Path) -> None:
    # Break caught: removing value predicates but leaving only predicate-dependent indexes.
    repository = _repository(tmp_path)
    statements = {
        "snapshot_runs_by_identity": (
            "SELECT * FROM snapshot_runs "
            "WHERE run_kind = ? AND idempotency_identity = ?",
            ("ANALYTICAL_SNAPSHOT", "a" * 64),
        ),
        "snapshot_runs_by_preimage": (
            "SELECT * FROM snapshot_runs WHERE run_kind = ? "
            "AND request_fingerprint = ? "
            "AND client_idempotency_key_digest IS ? AND book_id IS ? "
            "AND captured_generation IS ? AND target_cut_utc IS ?",
            ("SYNC", "a" * 64, None, None, None, None),
        ),
        "snapshot_runs_by_book_generation": (
            "SELECT * FROM snapshot_runs "
            "WHERE book_id = ? AND captured_generation = ?",
            ("book-alpha", 1),
        ),
        "snapshot_manifests_by_book_generation": (
            "SELECT * FROM snapshot_manifests "
            "WHERE book_id = ? AND book_generation > ? LIMIT 1",
            ("book-alpha", 1),
        ),
        "snapshot_manifests_by_book_sequence": (
            "SELECT * FROM snapshot_manifests "
            "WHERE book_id = ? ORDER BY publication_sequence DESC",
            ("book-alpha",),
        ),
        "recovery_events_by_book_sequence": (
            "SELECT * FROM snapshot_recovery_events "
            "WHERE book_id = ? ORDER BY event_sequence",
            ("book-alpha",),
        ),
    }
    with sqlite3.connect(repository.database_path) as connection:
        plans = {
            index_name: " ".join(
                row[3]
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
            )
            for index_name, (statement, parameters) in statements.items()
        }

    assert {
        index_name: index_name in plan for index_name, plan in plans.items()
    } == {index_name: True for index_name in statements}


@pytest.mark.parametrize(
    ("column", "assignment", "parameters"),
    [
        ("run_id", "run_id = ?", ("run_01J5X5S8J5J8P7KQ4Y0T3N6E1A",)),
        ("run_kind", "run_kind = ?", ("ANALYTICAL_SNAPSHOT_V2",)),
        ("idempotency_identity", "idempotency_identity = ?", ("b" * 64,)),
        ("request_fingerprint", "request_fingerprint = ?", ("b" * 64,)),
        (
            "client_idempotency_key_digest",
            "client_idempotency_key_digest = ?",
            ("b" * 64,),
        ),
        ("book_id", "book_id = ?", ("book-beta",)),
        ("captured_generation", "captured_generation = ?", (2,)),
        (
            "expected_active",
            "expected_active_snapshot_id = ?, expected_active_pointer_version = 1",
            (SNAPSHOT_A,),
        ),
        (
            "target_cut_utc",
            "target_cut_utc = ?",
            ("2026-08-20T08:01:00.000000Z",),
        ),
        (
            "requested_at_utc",
            "requested_at_utc = ?",
            ("2026-08-20T08:00:00.000000Z",),
        ),
    ],
)
def test_sql_makes_run_allocation_fields_immutable(
    tmp_path: Path,
    column: str,
    assignment: str,
    parameters: tuple[object, ...],
) -> None:
    # Break caught: ordinary SQL changing the server-derived allocation preimage.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    repository.advance_book_head("book-beta", 2, BOOK_REF_2, now=T0)
    run = repository.create_or_join(
        _new_run(client_idempotency_key=f"immutable-{column}"), now=T1
    ).record

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError) as captured:
            connection.execute(
                f"UPDATE snapshot_runs SET {assignment} WHERE run_id = ?",
                (*parameters, run.run_id),
            )

    assert "run allocation fields are immutable" in str(captured.value)


def test_allocation_immutability_trigger_preserves_lifecycle_updates(
    tmp_path: Path,
) -> None:
    # Protective regression: allocation immutability must not freeze worker-owned state.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="mutable-lifecycle"), now=T0
    ).record

    started = repository.claim_start(
        run.run_id, expected_version=run.version, now=T1
    )
    cancelled = repository.request_cancel(started.run_id, now=T2)

    assert started.run_stage is RunStage.INGESTING
    assert cancelled.cancel_requested_at_utc == T2


@pytest.mark.parametrize(
    "source_state",
    ["live_unreferenced", "terminal_unreferenced", "referenced"],
)
def test_insert_or_replace_cannot_rewrite_existing_run_id(
    tmp_path: Path,
    source_state: str,
) -> None:
    # Break caught: delete-then-insert replacing one logical run with a new preimage.
    repository = _repository(tmp_path)
    if source_state == "referenced":
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        publishing = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
        repository.commit_publication(
            publishing.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=publishing.version,
            now=T3,
        )
        source = repository.get(publishing.run_id)
    else:
        source = repository.create_or_join(
            _new_run(
                book_id=None,
                client_idempotency_key=None,
            ),
            now=T0,
        ).record
        if source_state == "terminal_unreferenced":
            source = repository.mark_failed(
                source.run_id,
                RunFailureV1(code=RunErrorCode.WORKER_FAILED),
                expected_version=source.version,
                now=T1,
            )

    replacement_fingerprint = "d" * 64
    replacement_request = _new_run(
        run_id=source.run_id,
        request_fingerprint=replacement_fingerprint,
        client_idempotency_key=None,
        book_id=source.book_id,
    )
    replacement_identity = RunRepository._idempotency_identity(
        replacement_request,
        source.captured_generation,
        None,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError) as captured:
            _insert_or_replace_run_from_existing(
                connection,
                source.run_id,
                overrides={
                    "idempotency_identity": replacement_identity,
                    "request_fingerprint": replacement_fingerprint,
                    "client_idempotency_key_digest": None,
                },
            )

    assert "run identity is immutable" in str(captured.value)
    assert repository.get(source.run_id).request_fingerprint == source.request_fingerprint


def test_insert_or_replace_cannot_hide_existing_run_behind_positive_rowid(
    tmp_path: Path,
) -> None:
    # Break caught: reintroducing a hidden rowid that can evict a terminal run.
    repository = _repository(tmp_path)
    source = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=None),
        now=T0,
    ).record
    source = repository.mark_failed(
        source.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=source.version,
        now=T1,
    )
    replacement_id = "run_01J5X5S8J5J8P7KQ4Y0T3N6F1A"
    replacement_fingerprint = "d" * 64
    replacement_identity = RunRepository._idempotency_identity(
        _new_run(
            run_id=replacement_id,
            book_id=None,
            request_fingerprint=replacement_fingerprint,
            client_idempotency_key=None,
        ),
        None,
        None,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError, match="rowid"):
            _insert_or_replace_run_from_existing(
                connection,
                source.run_id,
                explicit_rowid=1,
                overrides={
                    "run_id": replacement_id,
                    "idempotency_identity": replacement_identity,
                    "request_fingerprint": replacement_fingerprint,
                },
            )

    assert repository.get(source.run_id).run_id == source.run_id
    with pytest.raises(RunNotFoundError):
        repository.get(replacement_id)


@pytest.mark.parametrize("collision", ["live_identity", "live_book_generation"])
def test_insert_or_replace_cannot_evict_live_allocation_conflicts(
    tmp_path: Path,
    collision: str,
) -> None:
    # Break caught: OR REPLACE turning either partial live uniqueness rule into deletion.
    repository = _repository(tmp_path)
    if collision == "live_book_generation":
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        source = repository.create_or_join(
            _new_run(client_idempotency_key=None),
            now=T1,
        ).record
        replacement_fingerprint = "d" * 64
        replacement_identity = RunRepository._idempotency_identity(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6F1B",
                request_fingerprint=replacement_fingerprint,
                client_idempotency_key=None,
            ),
            1,
            None,
        )
        overrides = {
            "run_id": "run_01J5X5S8J5J8P7KQ4Y0T3N6F1B",
            "idempotency_identity": replacement_identity,
            "request_fingerprint": replacement_fingerprint,
        }
    else:
        source = repository.create_or_join(
            _new_run(book_id=None, client_idempotency_key=None),
            now=T0,
        ).record
        overrides = {"run_id": "run_01J5X5S8J5J8P7KQ4Y0T3N6F1C"}

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError) as captured:
            _insert_or_replace_run_from_existing(
                connection,
                source.run_id,
                overrides=overrides,
            )

    assert "run identity is immutable" in str(captured.value)
    assert repository.get(source.run_id).run_outcome is RunOutcome.RUNNING


@pytest.mark.parametrize("neighborhood", ["identity", "book_generation"])
def test_run_insert_collision_trigger_allows_fresh_and_terminal_rows(
    tmp_path: Path,
    neighborhood: str,
) -> None:
    # Protective regression: only live conflicts collide; terminal history remains appendable.
    repository = _repository(tmp_path)
    if neighborhood == "book_generation":
        repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
        source = repository.create_or_join(
            _new_run(client_idempotency_key=None),
            now=T1,
        ).record
        terminal_fingerprint = "d" * 64
        terminal_identity = RunRepository._idempotency_identity(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6F1D",
                request_fingerprint=terminal_fingerprint,
                client_idempotency_key=None,
            ),
            1,
            None,
        )
        allocation_overrides = {
            "run_id": "run_01J5X5S8J5J8P7KQ4Y0T3N6F1D",
            "idempotency_identity": terminal_identity,
            "request_fingerprint": terminal_fingerprint,
        }
    else:
        source = repository.create_or_join(
            _new_run(book_id=None, client_idempotency_key=None),
            now=T0,
        ).record
        allocation_overrides = {
            "run_id": "run_01J5X5S8J5J8P7KQ4Y0T3N6F1E",
        }
    terminal_id = str(allocation_overrides["run_id"])
    with sqlite3.connect(repository.database_path) as connection:
        _insert_or_replace_run_from_existing(
            connection,
            source.run_id,
            overrides={
                **allocation_overrides,
                "run_outcome": "FAILED",
                "updated_at_utc": "2026-08-20T08:02:00.000000Z",
                "finished_at_utc": "2026-08-20T08:02:00.000000Z",
                "error_code": "WORKER_FAILED",
                "error_message": "worker execution failed",
            },
        )

    assert repository.get(source.run_id).run_outcome is RunOutcome.RUNNING
    assert repository.get(terminal_id).run_outcome is RunOutcome.FAILED
    fresh = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6F1F",
            book_id=None,
            request_fingerprint="f" * 64,
            client_idempotency_key="fresh-after-trigger",
        ),
        now=T2,
    )
    assert fresh.created is True


def test_run_insert_collision_trigger_allows_repeated_fresh_runs(
    tmp_path: Path,
) -> None:
    # Protective regression: logical collision guards must not block unrelated fresh runs.
    repository = _repository(tmp_path)
    created_ids = []
    for index in range(64):
        run_id = f"run_implicit_{index:08d}"
        created = repository.create_or_join(
            _new_run(
                run_id=run_id,
                book_id=None,
                request_fingerprint=f"{index + 4_000_000:064x}",
                client_idempotency_key=None,
            ),
            now=T0,
        )
        assert created.created is True
        created_ids.append(created.record.run_id)

    assert len(created_ids) == 64
    assert len(repository.list_runs()) == 64


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("idempotency_identity", "b" * 64),
        ("request_fingerprint", "b" * 64),
    ],
)
def test_target_and_full_audit_recompute_stored_idempotency_identity(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    # Break caught: a valid-shaped stored identity/preimage drifting from canonical derivation.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key=f"attest-{column}"), now=T0
    ).record
    _execute_without_named_trigger(
        repository,
        "snapshot_run_allocation_immutable",
        f"UPDATE snapshot_runs SET {column} = ? WHERE run_id = ?",
        (value, run.run_id),
    )

    with pytest.raises(RunDatabaseError):
        repository.get(run.run_id)
    with pytest.raises(RunDatabaseError):
        repository.audit_integrity()


@pytest.mark.parametrize(
    ("corruption", "assignment"),
    [
        ("identity", "idempotency_identity = ?"),
        ("preimage", "request_fingerprint = ?"),
    ],
)
def test_create_or_join_validates_identity_and_exact_preimage_neighborhoods(
    tmp_path: Path,
    corruption: str,
    assignment: str,
) -> None:
    # Break caught: corruption moving a logically identical non-book run out of lookup sight.
    repository = _repository(tmp_path)
    request = _new_run(
        book_id=None,
        client_idempotency_key=f"dual-neighborhood-{corruption}",
    )
    first = repository.create_or_join(request, now=T0).record
    _execute_without_named_trigger(
        repository,
        "snapshot_run_allocation_immutable",
        f"UPDATE snapshot_runs SET {assignment} WHERE run_id = ?",
        ("b" * 64, first.run_id),
    )

    with pytest.raises(RunDatabaseError):
        repository.create_or_join(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E1B",
                book_id=None,
                client_idempotency_key=f"dual-neighborhood-{corruption}",
            ),
            now=T1,
        )

    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_runs").fetchone()[0] == 1


@pytest.mark.parametrize("ancestor_corruption", ["invalid_owner", "identity_mismatch"])
def test_expected_active_provenance_walk_reaches_deep_ancestors(
    tmp_path: Path,
    ancestor_corruption: str,
) -> None:
    # Break caught: target, active, and event reads stopping at the immediate owner tuple.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2A",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    dependent = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2B",
            client_idempotency_key=f"deep-dependent-{ancestor_corruption}",
        ),
        now=T5,
    ).record
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', NULL,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A,),
        )

    if ancestor_corruption == "invalid_owner":
        invalid_owner = _failed_nonbook_run(
            repository,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2C",
            client_idempotency_key="deep-invalid-owner",
        )
        _execute_without_named_triggers(
            repository,
            _MANIFEST_UPDATE_BYPASS,
            "UPDATE snapshot_manifests SET run_id = ? WHERE snapshot_id = ?",
            (invalid_owner.run_id, SNAPSHOT_A),
            foreign_keys=True,
        )
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    else:
        _execute_without_named_triggers(
            repository,
            _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
            "UPDATE snapshot_runs SET request_fingerprint = ? WHERE run_id = ?",
            ("f" * 64, first.run_id),
        )

    with pytest.raises(RunDatabaseError):
        repository.get(dependent.run_id)
    with pytest.raises(RunDatabaseError):
        repository.get_active("book-alpha")
    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")


def test_expected_active_provenance_walk_accepts_equal_timestamp_multihop(
    tmp_path: Path,
) -> None:
    # Protective regression: equality is valid when several publications share one cut.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2D",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T3,
    )
    third = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E2E",
        snapshot_id=SNAPSHOT_C,
    )
    repository.commit_publication(
        third.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=third.version,
        now=T3,
    )

    assert repository.get(third.run_id).expected_active_snapshot_id == SNAPSHOT_B
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_C
    repository.audit_integrity()


@pytest.mark.parametrize("surface", ["run", "active", "audit"])
def test_published_owner_cannot_expect_a_forward_publication_sequence(
    tmp_path: Path,
    surface: str,
) -> None:
    # Break caught: a sequence-one owner accepting sequence two at the same timestamp.
    repository = _repository(tmp_path)
    snapshot_ids, run_ids = _seed_publication_chain(repository, 2)
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        """
        UPDATE snapshot_runs
        SET expected_active_snapshot_id = CASE
                WHEN run_id = ? THEN ?
                ELSE NULL
            END,
            expected_active_pointer_version = CASE
                WHEN run_id = ? THEN 2
                ELSE 0
            END
        WHERE run_id IN (?, ?)
        """,
        (run_ids[0], snapshot_ids[1], run_ids[0], run_ids[0], run_ids[1]),
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE active_snapshots
            SET snapshot_id = ?, pointer_version = 3
            WHERE book_id = 'book-alpha'
            """,
            (snapshot_ids[0],),
        )

    with pytest.raises(RunDatabaseError, match="publication sequence"):
        if surface == "run":
            repository.get(run_ids[0])
        elif surface == "active":
            repository.get_active("book-alpha")
        else:
            repository.audit_integrity()


def test_nonpublished_run_may_reference_an_expected_publication(
    tmp_path: Path,
) -> None:
    # Protective regression: sequence ordering applies only to a publication owner's edge.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    published = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        published.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=published.version,
        now=T3,
    )
    queued = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6F2A",
            client_idempotency_key="nonpublished-expected",
        ),
        now=T3,
    ).record

    assert repository.get(queued.run_id).expected_active_snapshot_id == SNAPSHOT_A
    repository.audit_integrity()


@pytest.mark.parametrize("operation", ["audit", "initialize"])
def test_full_catalog_publication_ancestry_scales_linearly(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: every audit root rewalking the complete expected-publication tail.
    costs: dict[int, int] = {}
    for count in (100, 400):
        path = tmp_path / f"{operation}-{count}"
        seed = _repository(path)
        _seed_publication_chain(seed, count)
        measured = _VmBudgetRunRepository(path)
        measured.reset_vm_callbacks()
        if operation == "audit":
            measured.audit_integrity()
        else:
            measured.initialize()
        costs[count] = measured.vm_callbacks

    assert costs[400] <= costs[100] * 6, costs


@pytest.mark.parametrize("operation", ["list_publications", "recover_active"])
def test_multirow_publication_ancestry_reads_scale_linearly(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: each row in one read transaction starting a fresh ancestry walk.
    costs: dict[int, int] = {}
    for count in (100, 400):
        path = tmp_path / f"{operation}-{count}"
        seed = _repository(path)
        _seed_publication_chain(seed, count)
        measured = _VmBudgetRunRepository(path)
        measured.reset_vm_callbacks()
        if operation == "list_publications":
            assert len(measured.list_publications("book-alpha")) == count
        else:
            recovered = measured.recover_active(
                "book-alpha",
                verify=lambda _snapshot_id: (_ for _ in ()).throw(
                    ValueError("corrupt")
                ),
                now=T1,
            )
            assert recovered.decision is ActiveRecoveryDecision.REMOVED
        costs[count] = measured.vm_callbacks

    assert costs[400] <= costs[100] * 6, costs


def test_failed_deep_publication_walk_does_not_poison_completed_tails(
    tmp_path: Path,
) -> None:
    # Break caught: a failed walk memoizing its valid prefix as a proven ancestry tail.
    repository = _repository(tmp_path)
    snapshot_ids, run_ids = _seed_publication_chain(repository, 400)
    missing_snapshot_id = "f" * 64
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        """
        UPDATE snapshot_runs
        SET expected_active_snapshot_id = ?, expected_active_pointer_version = 1
        WHERE run_id = ?
        """,
        (missing_snapshot_id, run_ids[0]),
    )

    completed: set[tuple[str, str]] = set()
    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM snapshot_manifests WHERE snapshot_id = ?",
            (snapshot_ids[-1],),
        ).fetchone()
        assert row is not None
        publication = RunRepository._publication_from_row(row)
        with pytest.raises(RunDatabaseError, match="missing"):
            RunRepository._validate_publication_relations(
                connection,
                publication,
                completed_tails=completed,
            )

    assert completed == set()


def test_deep_publication_chain_cycle_is_not_hidden_by_completed_tails(
    tmp_path: Path,
) -> None:
    # Protective regression: per-walk visiting state must remain separate from proven tails.
    repository = _repository(tmp_path)
    snapshot_ids, run_ids = _seed_publication_chain(repository, 400)
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        """
        UPDATE snapshot_runs
        SET expected_active_snapshot_id = ?, expected_active_pointer_version = 201
        WHERE run_id = ?
        """,
        (snapshot_ids[200], run_ids[0]),
    )

    with pytest.raises(RunDatabaseError):
        repository.get_active("book-alpha")
    with pytest.raises(RunDatabaseError):
        repository.audit_integrity()


def test_expected_active_provenance_walk_rejects_real_cycle(tmp_path: Path) -> None:
    # Break caught: mutually expected publications looping forever or passing one-edge checks.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run_a = _new_run(
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E3A",
        request_fingerprint="a" * 64,
        client_idempotency_key=None,
    )
    run_b = _new_run(
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E3B",
        request_fingerprint="b" * 64,
        client_idempotency_key=None,
    )
    identity_a = RunRepository._idempotency_identity(run_a, 1, None)
    identity_b = RunRepository._idempotency_identity(run_b, 1, None)
    timestamp = "2026-08-20T08:00:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
        for request, identity, candidate, expected, pointer_version in (
            (run_a, identity_a, SNAPSHOT_A, SNAPSHOT_B, 2),
            (run_b, identity_b, SNAPSHOT_B, SNAPSHOT_A, 1),
        ):
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
                    ?, 'ANALYTICAL_SNAPSHOT', ?, ?, NULL, 'book-alpha', 1,
                    ?, ?, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                    NULL, ?, ?, NULL, NULL, NULL, 1
                )
                """,
                (
                    request.run_id,
                    identity,
                    request.request_fingerprint,
                    expected,
                    pointer_version,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    candidate,
                    candidate,
                ),
            )
        for request, snapshot_id, envelope in (
            (run_a, SNAPSHOT_A, "e" * 64),
            (run_b, SNAPSHOT_B, "d" * 64),
        ):
            connection.execute(
                """
                INSERT INTO snapshot_manifests (
                    snapshot_id, run_id, book_id, book_generation, snapshot_status,
                    schema_version, hash_algorithm, manifest_relpath, envelope_sha256,
                    envelope_byte_length, published_at_utc
                ) VALUES (?, ?, 'book-alpha', 1, 'BLESSED',
                    'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096, ?)
                """,
                (
                    snapshot_id,
                    request.run_id,
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{snapshot_id[:2]}/{snapshot_id}.json",
                    envelope,
                    timestamp,
                ),
            )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = 2, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (SNAPSHOT_B, timestamp),
        foreign_keys=True,
    )

    with pytest.raises(RunDatabaseError, match="cycle"):
        repository.get_active("book-alpha")
    with pytest.raises(RunDatabaseError, match="cycle"):
        repository.audit_integrity()


def test_expected_active_provenance_walk_rejects_self_cycle(tmp_path: Path) -> None:
    # Break caught: a publication owner naming its own publication as expected-active.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    request = _new_run(
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E3C",
        client_idempotency_key=None,
    )
    identity = RunRepository._idempotency_identity(request, 1, None)
    timestamp = "2026-08-20T08:00:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
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
                ?, 'ANALYTICAL_SNAPSHOT', ?, ?, NULL, 'book-alpha', 1,
                ?, 1, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                NULL, ?, ?, NULL, NULL, NULL, 1
            )
            """,
            (
                request.run_id,
                identity,
                request.request_fingerprint,
                SNAPSHOT_A,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                SNAPSHOT_A,
                SNAPSHOT_A,
            ),
        )
        connection.execute(
            """
            INSERT INTO snapshot_manifests (
                snapshot_id, run_id, book_id, book_generation, snapshot_status,
                schema_version, hash_algorithm, manifest_relpath, envelope_sha256,
                envelope_byte_length, published_at_utc
            ) VALUES (?, ?, 'book-alpha', 1, 'BLESSED',
                'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096, ?)
            """,
            (
                SNAPSHOT_A,
                request.run_id,
                "snapshots/manifests/analytical_snapshot_manifest_v1/"
                f"{SNAPSHOT_A[:2]}/{SNAPSHOT_A}.json",
                "e" * 64,
                timestamp,
            ),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = 1, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (SNAPSHOT_A, timestamp),
        foreign_keys=True,
    )

    with pytest.raises(RunDatabaseError, match="cycle"):
        repository.get_active("book-alpha")
    with pytest.raises(RunDatabaseError, match="cycle"):
        repository.audit_integrity()


def test_publication_provenance_walk_allows_normal_owner_back_edge(
    tmp_path: Path,
) -> None:
    # Protective regression: owner -> owned publication is not an expected-active cycle.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    assert repository.get(run.run_id).published_snapshot_id == SNAPSHOT_A
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A


@pytest.mark.parametrize("publication_count", [1, 2])
def test_timestamp_only_pointer_drift_is_rejected_before_recovery_cas(
    tmp_path: Path,
    publication_count: int,
) -> None:
    # Break caught: a timestamp-only rewrite fabricating pointer drift outside a revision.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    if publication_count == 2:
        _publish_second_alpha(repository)
    before = repository.get_active("book-alpha")

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                "UPDATE active_snapshots SET updated_at_utc = ? "
                "WHERE book_id = 'book-alpha'",
                ("2026-08-20T08:05:00.000000Z",),
            )

    assert repository.get_active("book-alpha") == before


def test_insert_or_replace_cannot_replace_immutable_manifest_identity(
    tmp_path: Path,
) -> None:
    # Break caught: REPLACE bypassing update triggers and degrading selected evidence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E4B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO snapshot_recovery_events (
                book_id, rejected_snapshot_id, expected_pointer_version,
                resolution_action, selected_snapshot_id, detail_json,
                recorded_at_utc
            ) VALUES (
                'book-alpha', ?, 1, 'CAS_LOST', ?,
                '{"failures":[],"omitted_count":0}',
                '2026-08-20T08:05:00.000000Z'
            )
            """,
            (SNAPSHOT_A, SNAPSHOT_B),
        )
        row = connection.execute(
            "SELECT * FROM snapshot_manifests WHERE snapshot_id = ?",
            (SNAPSHOT_B,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError) as captured:
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshot_manifests (
                    publication_sequence, snapshot_id, run_id, book_id,
                    book_generation, snapshot_status, schema_version,
                    hash_algorithm, manifest_relpath, envelope_sha256,
                    envelope_byte_length, published_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'DEGRADED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                ),
            )

    assert "manifest identity is immutable" in str(captured.value)
    assert repository.list_publications("book-alpha")[-1].snapshot_status is SnapshotStatus.BLESSED
    assert repository.list_recovery_events("book-alpha")[0].selected_snapshot_id == SNAPSHOT_B


def test_insert_or_replace_cannot_replace_positive_publication_sequence(
    tmp_path: Path,
) -> None:
    # Break caught: a fresh snapshot/run pair replacing history by explicit sequence alone.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    replacement = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6E4C",
        snapshot_id=SNAPSHOT_C,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            UPDATE snapshot_runs
            SET run_outcome = 'SUCCEEDED', published_snapshot_id = ?,
                updated_at_utc = ?, finished_at_utc = ?, version = version + 1
            WHERE run_id = ?
            """,
            (
                SNAPSHOT_C,
                "2026-08-20T08:03:00.000000Z",
                "2026-08-20T08:03:00.000000Z",
                replacement.run_id,
            ),
        )
        publication_sequence = connection.execute(
            "SELECT publication_sequence FROM snapshot_manifests WHERE snapshot_id = ?",
            (SNAPSHOT_A,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError) as captured:
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshot_manifests (
                    publication_sequence, snapshot_id, run_id, book_id,
                    book_generation, snapshot_status, schema_version,
                    hash_algorithm, manifest_relpath, envelope_sha256,
                    envelope_byte_length, published_at_utc
                ) VALUES (?, ?, ?, 'book-alpha', 1, 'BLESSED',
                    'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096, ?)
                """,
                (
                    publication_sequence,
                    SNAPSHOT_C,
                    replacement.run_id,
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{SNAPSHOT_C[:2]}/{SNAPSHOT_C}.json",
                    "d" * 64,
                    "2026-08-20T08:03:00.000000Z",
                ),
            )

    assert "manifest identity is immutable" in str(captured.value)
    assert repository.list_publications("book-alpha")[0].snapshot_id == SNAPSHOT_A


def test_create_or_join_neighborhood_queries_use_named_indexes_without_sort(
    tmp_path: Path,
) -> None:
    # Break caught: complete-neighborhood reads sorting or missing the exact preimage lookup.
    repository = _TracingVmRunRepository(tmp_path)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    repository.statements.clear()

    repository.create_or_join(
        _new_run(client_idempotency_key="trace-neighborhoods"), now=T1
    )

    statements = [" ".join(statement.split()) for statement in repository.statements]
    identity = [
        statement
        for statement in statements
        if statement.startswith("SELECT * FROM snapshot_runs")
        and "WHERE run_kind =" in statement
        and "idempotency_identity =" in statement
    ]
    preimage = [
        statement
        for statement in statements
        if statement.startswith("SELECT * FROM snapshot_runs")
        and "request_fingerprint =" in statement
        and "client_idempotency_key_digest" in statement
        and "WHERE run_kind =" in statement
    ]
    generation = [
        statement
        for statement in statements
        if statement.startswith("SELECT * FROM snapshot_runs")
        and "WHERE book_id =" in statement
        and "captured_generation =" in statement
    ]
    assert len(identity) == len(preimage) == len(generation) == 1
    assert all("ORDER BY" not in statement for statement in (*identity, *preimage, *generation))

    expected_indexes = {
        identity[0]: "snapshot_runs_by_identity",
        preimage[0]: "snapshot_runs_by_preimage",
        generation[0]: "snapshot_runs_by_book_generation",
    }
    with sqlite3.connect(repository.database_path) as connection:
        for statement, index_name in expected_indexes.items():
            plan = " ".join(
                row[3]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")
            )
            assert index_name in plan
            assert "TEMP B-TREE" not in plan


def test_identity_and_preimage_lookups_remain_constant_with_100k_unrelated_rows(
    tmp_path: Path,
) -> None:
    # Break caught: adding complete preimage validation as an unindexed catalog scan.
    small = _VmBudgetRunRepository(tmp_path / "small-preimage")
    large = _VmBudgetRunRepository(tmp_path / "large-preimage")
    for repository, count in ((small, 100), (large, 100_000)):
        repository.initialize()
        _seed_nonbook_history(repository, count)

    def allocation_cost(repository: _VmBudgetRunRepository, suffix: str) -> int:
        repository.reset_vm_callbacks()
        repository.create_or_join(
            _new_run(
                run_id=f"run_01J5X5S8J5J8P7KQ4Y0T3N6{suffix}",
                book_id=None,
                request_fingerprint="f" * 64,
                client_idempotency_key=f"preimage-scale-{suffix}",
            ),
            now=T0,
        )
        return repository.vm_callbacks

    small_cost = allocation_cost(small, "E5A")
    large_cost = allocation_cost(large, "E5B")

    assert large_cost <= small_cost + 100


def test_manifest_envelope_metadata_is_immutable_after_publication(
    tmp_path: Path,
) -> None:
    # Break caught: ordinary SQL rewriting attested envelope metadata after publication.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_manifests SET envelope_sha256 = ? WHERE snapshot_id = ?",
                ("d" * 64, SNAPSHOT_A),
            )

    publication = repository.list_publications("book-alpha")[0]
    assert publication.envelope_sha256 == "e" * 64
    repository.audit_integrity()


def test_manifest_cannot_be_deleted_after_publication(tmp_path: Path) -> None:
    # Break caught: an unpointed publication being erased from immutable history.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM snapshot_manifests WHERE snapshot_id = ?",
                (SNAPSHOT_A,),
            )

    assert repository.list_publications("book-alpha")[0].snapshot_id == SNAPSHOT_A


def test_recovery_event_cannot_be_updated(tmp_path: Path) -> None:
    # Break caught: ordinary SQL rewriting append-only recovery evidence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(connection, rejected_snapshot_id=SNAPSHOT_A)

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_recovery_events
                SET detail_json = '{"failures":[],"omitted_count":1}'
                WHERE event_sequence = 1
                """
            )

    assert json.loads(repository.list_recovery_events("book-alpha")[0].detail_json) == {
        "failures": [],
        "omitted_count": 0,
    }


def test_recovery_event_cannot_be_deleted(tmp_path: Path) -> None:
    # Break caught: ordinary SQL erasing append-only recovery evidence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(connection, rejected_snapshot_id=SNAPSHOT_A)

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM snapshot_recovery_events WHERE event_sequence = 1"
            )

    assert len(repository.list_recovery_events("book-alpha")) == 1


def test_insert_or_replace_cannot_rewrite_recovery_event_sequence(
    tmp_path: Path,
) -> None:
    # Break caught: REPLACE deleting and reinserting evidence while recursive triggers are off.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(connection, rejected_snapshot_id=SNAPSHOT_A)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_recovery_event(
                connection,
                rejected_snapshot_id=SNAPSHOT_A,
                expected_pointer_version=1,
                event_sequence=1,
                replace=True,
            )

    assert repository.list_recovery_events("book-alpha")[0].expected_pointer_version == 1


def test_recovery_event_sequences_allow_implicit_positive_append(
    tmp_path: Path,
) -> None:
    # Protective regression: insert guards must accept omitted AUTOINCREMENT identities.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(connection, rejected_snapshot_id=SNAPSHOT_A)
        _insert_raw_recovery_event(
            connection,
            rejected_snapshot_id=SNAPSHOT_A,
            expected_pointer_version=1,
        )
        sequences = tuple(
            row[0]
            for row in connection.execute(
                "SELECT event_sequence FROM snapshot_recovery_events ORDER BY event_sequence"
            )
        )

    assert sequences == (1, 2)


@pytest.mark.parametrize("event_sequence", [0, -1])
def test_recovery_event_sequence_must_be_positive(
    tmp_path: Path,
    event_sequence: int,
) -> None:
    # Break caught: explicit nonpositive identities entering an append-only ledger.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_recovery_event(
                connection,
                rejected_snapshot_id=SNAPSHOT_A,
                event_sequence=event_sequence,
            )


def test_recovery_event_cannot_backfill_below_high_water_mark(tmp_path: Path) -> None:
    # Break caught: explicit raw SQL inserting older evidence after a later sequence exists.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    _publish_second_alpha(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(
            connection,
            rejected_snapshot_id=SNAPSHOT_A,
            event_sequence=2,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_recovery_event(
                connection,
                rejected_snapshot_id=SNAPSHOT_A,
                expected_pointer_version=1,
                event_sequence=1,
            )


def test_terminal_run_cannot_be_coherently_updated_by_sql(tmp_path: Path) -> None:
    # Break caught: direct SQL rewriting a terminal failure into different evidence.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="terminal-update"), now=T0
    ).record
    terminal = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T1,
    )

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE snapshot_runs
                SET error_code = 'DISK_WRITE_FAILED',
                    error_message = 'durable artifact write failed',
                    version = version + 1
                WHERE run_id = ?
                """,
                (run.run_id,),
            )

    assert repository.get(run.run_id) == terminal


def test_terminal_run_cannot_be_deleted_by_sql(tmp_path: Path) -> None:
    # Break caught: direct SQL erasing unreferenced terminal run evidence.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="terminal-delete"), now=T0
    ).record
    terminal = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T1,
    )

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM snapshot_runs WHERE run_id = ?", (run.run_id,))

    assert repository.get(run.run_id) == terminal


def test_running_run_cannot_be_deleted_by_sql(tmp_path: Path) -> None:
    # Break caught: direct SQL erasing admitted work before its terminal evidence exists.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="running-delete"), now=T0
    ).record

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM snapshot_runs WHERE run_id = ?", (run.run_id,))

    assert repository.get(run.run_id) == run


def test_terminal_run_guard_preserves_running_to_terminal_transition(
    tmp_path: Path,
) -> None:
    # Protective regression: immutability begins after, not before, terminalization.
    repository = _repository(tmp_path)
    run = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="terminal-transition"), now=T0
    ).record

    terminal = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T1,
    )

    assert terminal.run_outcome is RunOutcome.FAILED
    assert terminal.error_code is RunErrorCode.WORKER_FAILED


def test_stale_active_pointer_wins_over_rolled_back_publisher_clock(
    tmp_path: Path,
) -> None:
    # Break caught: stale caller time masking an already-lost active-pointer CAS.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I4A",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    stale = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I4B",
        snapshot_id=SNAPSHOT_C,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    repository.recover_active("book-alpha", verify=verify, now=T5)

    rejected = repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=stale.version,
        now=T2,
    )

    assert rejected.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert rejected.run.run_outcome is RunOutcome.FAILED
    assert rejected.run.updated_at_utc == T5
    assert rejected.run.finished_at_utc == T5
    assert repository.get_active("book-alpha").snapshot_id == SNAPSHOT_A


def test_matching_active_pointer_still_rejects_rolled_back_publisher_clock(
    tmp_path: Path,
) -> None:
    # Protective regression: pointer-ordering repair must not permit ordinary time travel.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    matching = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I4C",
        snapshot_id=SNAPSHOT_B,
    )

    with pytest.raises(RunDatabaseError):
        repository.commit_publication(
            matching.run_id,
            _publication(SNAPSHOT_B, generation=1),
            expected_version=matching.version,
            now=T2,
        )

    assert repository.get(matching.run_id) == matching


def test_recover_interrupted_floors_each_live_run_time_atomically(
    tmp_path: Path,
) -> None:
    # Break caught: startup clock rollback leaving all durable work falsely RUNNING.
    repository = _repository(tmp_path)
    first = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I5A",
            request_fingerprint="5" * 64,
            client_idempotency_key="interrupt-floor-a",
            book_id=None,
        ),
        now=T3,
    ).record
    second = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I5B",
            request_fingerprint="6" * 64,
            client_idempotency_key="interrupt-floor-b",
            book_id=None,
        ),
        now=T4,
    ).record

    recovered_ids = repository.recover_interrupted(now=T1)

    assert recovered_ids == (first.run_id, second.run_id)
    recovered_first = repository.get(first.run_id)
    recovered_second = repository.get(second.run_id)
    assert recovered_first.run_outcome is RunOutcome.FAILED
    assert recovered_first.error_code is RunErrorCode.INTERRUPTED
    assert recovered_first.updated_at_utc == T3
    assert recovered_first.finished_at_utc == T3
    assert recovered_second.run_outcome is RunOutcome.FAILED
    assert recovered_second.error_code is RunErrorCode.INTERRUPTED
    assert recovered_second.updated_at_utc == T4
    assert recovered_second.finished_at_utc == T4


@pytest.mark.parametrize("later_reference", ["rejected", "selected"])
def test_recovery_event_insert_rejects_time_before_referenced_publication(
    tmp_path: Path,
    later_reference: str,
) -> None:
    # Break caught: new evidence claiming to exist before a referenced publication.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I6A",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    rejected_id, selected_id = (
        (SNAPSHOT_B, SNAPSHOT_A)
        if later_reference == "rejected"
        else (SNAPSHOT_A, SNAPSHOT_B)
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_recovery_event(
                connection,
                rejected_snapshot_id=rejected_id,
                selected_snapshot_id=selected_id,
                expected_pointer_version=2,
                recorded_at_utc="2026-08-20T08:03:00.000000Z",
            )


@pytest.mark.parametrize("later_reference", ["rejected", "selected"])
def test_recovery_event_read_and_audit_reject_bypassed_time_travel(
    tmp_path: Path,
    later_reference: str,
) -> None:
    # Break caught: trigger-bypassed temporal corruption passing decoded/full audit checks.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I6B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    rejected_id, selected_id = (
        (SNAPSHOT_B, SNAPSHOT_A)
        if later_reference == "rejected"
        else (SNAPSHOT_A, SNAPSHOT_B)
    )
    _execute_without_named_triggers(
        repository,
        (
            "recovery_event_time_not_before_publications",
            "recovery_event_pointer_causal_on_insert",
        ),
        """
        INSERT INTO snapshot_recovery_events (
            book_id, rejected_snapshot_id, expected_pointer_version,
            resolution_action, selected_snapshot_id, detail_json, recorded_at_utc
        ) VALUES (
            'book-alpha', ?, 2, 'CAS_LOST', ?,
            '{"failures":[],"omitted_count":0}',
            '2026-08-20T08:03:00.000000Z'
        )
        """,
        (rejected_id, selected_id),
    )

    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")
    with pytest.raises(RunDatabaseError):
        repository.audit_integrity()


def test_nonbusy_sqlite_wal_failure_is_typed_and_closes_handle(
    tmp_path: Path,
) -> None:
    # Break caught: non-OperationalError SQLite failures escaping the repository boundary.
    repository = _NonOperationalWalErrorRepository(tmp_path)

    with pytest.raises(RunDatabaseError) as captured:
        repository.initialize()

    assert isinstance(captured.value.__cause__, sqlite3.DatabaseError)
    assert repository.wal_attempts == 1
    assert repository.opened_connections == repository.closed_connections


@pytest.mark.parametrize("operation", ["initialize", "audit"])
def test_full_integrity_check_rejects_missing_unique_index_entries(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: quick_check accepting a unique index whose b-tree omits a live row.
    repository = _repository(tmp_path)
    _damage_live_identity_index(repository)
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"

    with pytest.raises(RunDatabaseError):
        if operation == "initialize":
            RunRepository(tmp_path).initialize()
        else:
            repository.audit_integrity()


@pytest.mark.parametrize(
    "trigger_name",
    [
        "manifest_update_immutable",
        "manifest_delete_immutable",
        "recovery_event_identity_collision_on_insert",
        "recovery_event_update_immutable",
        "recovery_event_delete_immutable",
        "snapshot_run_terminal_update_immutable",
        "snapshot_run_delete_immutable",
        "recovery_event_time_not_before_publications",
        "recovery_event_pointer_causal_on_insert",
    ],
)
def test_initialize_attests_every_append_only_guard(
    tmp_path: Path,
    trigger_name: str,
) -> None:
    # Break caught: startup accepting a catalog after a durability guard was removed.
    repository = _repository(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


def test_recovery_event_time_equal_to_latest_reference_is_valid(
    tmp_path: Path,
) -> None:
    # Protective regression: temporal evidence ordering is inclusive, not strictly later.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I6C",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(
            connection,
            rejected_snapshot_id=SNAPSHOT_A,
            selected_snapshot_id=SNAPSHOT_B,
            expected_pointer_version=1,
            recorded_at_utc="2026-08-20T08:04:00.000000Z",
        )

    assert repository.list_recovery_events("book-alpha")[0].recorded_at_utc == T4
    repository.audit_integrity()


def test_manifest_sequences_allow_implicit_positive_append(tmp_path: Path) -> None:
    # Protective regression: high-water guards must accept omitted AUTOINCREMENT identities.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I7A",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )

    assert [
        publication.publication_sequence
        for publication in repository.list_publications("book-alpha")
    ] == [2, 1]


@pytest.mark.parametrize("publication_sequence", [0, -1])
def test_manifest_publication_sequence_must_be_positive(
    tmp_path: Path,
    publication_sequence: int,
) -> None:
    # Break caught: explicit nonpositive identities entering publication history.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    raw_owner = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I7D",
        snapshot_id=SNAPSHOT_B,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("SAVEPOINT invalid_sequence")
        try:
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'SUCCEEDED', published_snapshot_id = ?,
                    updated_at_utc = ?, finished_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (
                    SNAPSHOT_B,
                    "2026-08-20T08:04:00.000000Z",
                    "2026-08-20T08:04:00.000000Z",
                    raw_owner.run_id,
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO snapshot_manifests (
                        publication_sequence, snapshot_id, run_id, book_id,
                        book_generation, snapshot_status, schema_version,
                        hash_algorithm, manifest_relpath, envelope_sha256,
                        envelope_byte_length, published_at_utc
                    ) VALUES (
                        ?, ?, ?, 'book-alpha', 1, 'BLESSED',
                        'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096,
                        '2026-08-20T08:04:00.000000Z'
                    )
                    """,
                    (
                        publication_sequence,
                        SNAPSHOT_B,
                        raw_owner.run_id,
                        "snapshots/manifests/analytical_snapshot_manifest_v1/"
                        f"{SNAPSHOT_B[:2]}/{SNAPSHOT_B}.json",
                        "d" * 64,
                    ),
                )
        finally:
            connection.execute("ROLLBACK TO invalid_sequence")
            connection.execute("RELEASE invalid_sequence")


def test_manifest_cannot_backfill_below_high_water_mark(tmp_path: Path) -> None:
    # Break caught: explicit raw SQL inserting publication history below a later sequence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE sqlite_sequence SET seq = 9 WHERE name = 'snapshot_manifests'"
        )
    second = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I7B",
        snapshot_id=SNAPSHOT_B,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T4,
    )
    raw_owner = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6I7C",
        snapshot_id=SNAPSHOT_C,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("SAVEPOINT backfill_attack")
        try:
            connection.execute(
                """
                UPDATE snapshot_runs
                SET run_outcome = 'SUCCEEDED', published_snapshot_id = ?,
                    updated_at_utc = ?, finished_at_utc = ?, version = version + 1
                WHERE run_id = ?
                """,
                (
                    SNAPSHOT_C,
                    "2026-08-20T08:05:00.000000Z",
                    "2026-08-20T08:05:00.000000Z",
                    raw_owner.run_id,
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO snapshot_manifests (
                        publication_sequence, snapshot_id, run_id, book_id,
                        book_generation, snapshot_status, schema_version,
                        hash_algorithm, manifest_relpath, envelope_sha256,
                        envelope_byte_length, published_at_utc
                    ) VALUES (
                        2, ?, ?, 'book-alpha', 1, 'BLESSED',
                        'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096,
                        '2026-08-20T08:05:00.000000Z'
                    )
                    """,
                    (
                        SNAPSHOT_C,
                        raw_owner.run_id,
                        "snapshots/manifests/analytical_snapshot_manifest_v1/"
                        f"{SNAPSHOT_C[:2]}/{SNAPSHOT_C}.json",
                        "d" * 64,
                    ),
                )
        finally:
            connection.execute("ROLLBACK TO backfill_attack")
            connection.execute("RELEASE backfill_attack")


def test_new_book_has_private_virgin_pointer_register(tmp_path: Path) -> None:
    # Break caught: a newly created book representing pointer state through row absence.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)

    with sqlite3.connect(repository.database_path) as connection:
        register = connection.execute(
            "SELECT snapshot_id, book_generation, pointer_version, updated_at_utc "
            "FROM active_snapshots WHERE book_id = 'book-alpha'"
        ).fetchone()

    assert register == (None, None, 0, "2026-08-20T08:00:00.000000Z")
    assert repository.get_active("book-alpha") is None
    assert repository.list_active() == ()


def test_list_active_hides_tombstones_but_validates_their_registers(
    tmp_path: Path,
) -> None:
    # Break caught: startup enumeration silently skipping a corrupt private tombstone.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    run = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        run.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=run.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )
    assert repository.list_active() == ()

    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        "UPDATE active_snapshots SET pointer_version = 3 "
        "WHERE book_id = 'book-alpha'",
        (),
    )

    with pytest.raises(RunDatabaseError, match="pointer"):
        repository.list_active()


def test_pointer_register_rejects_delete_replace_and_skipped_revision(
    tmp_path: Path,
) -> None:
    # Break caught: SQL erasing, replacing, or jumping the durable register revision.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)

    with sqlite3.connect(repository.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM active_snapshots WHERE book_id = 'book-alpha'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO active_snapshots (
                    book_id, snapshot_id, book_generation,
                    pointer_version, updated_at_utc
                ) VALUES ('book-alpha', NULL, NULL, 0, ?)
                """,
                ("2026-08-20T08:00:00.000000Z",),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE active_snapshots
                SET pointer_version = 2, updated_at_utc = ?
                WHERE book_id = 'book-alpha'
                """,
                ("2026-08-20T08:01:00.000000Z",),
            )

    assert repository.get_active("book-alpha") is None
    repository.audit_integrity()


@pytest.mark.parametrize(
    "assignment",
    [
        "snapshot_id = NULL",
        "book_generation = NULL",
        (
            "pointer_version = pointer_version + 1, "
            "updated_at_utc = '2026-08-20T08:04:00.000000Z'"
        ),
    ],
)
def test_live_pointer_rejects_half_null_and_same_state_revision_bumps(
    tmp_path: Path,
    assignment: str,
) -> None:
    # Break caught: a malformed identity tuple or no-op state masquerading as a new epoch.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE active_snapshots SET {assignment} "
                "WHERE book_id = 'book-alpha'"
            )

    assert repository.get_active("book-alpha").pointer_version == 1


def test_generation_advance_preserves_pointer_epoch(tmp_path: Path) -> None:
    # Break caught: canonical-book generation movement resetting unrelated pointer state.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    published = repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T2,
    )

    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T3)

    assert repository.get_active("book-alpha") == published.active
    next_run = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J0B",
            client_idempotency_key="generation-preserves-pointer",
        ),
        now=T3,
    ).record
    assert next_run.captured_generation == 2
    assert next_run.expected_active_snapshot_id == SNAPSHOT_A
    assert next_run.expected_active_pointer_version == 1


@pytest.mark.parametrize(
    "operation",
    [
        "create_or_join",
        "get_book_head",
        "get_active",
        "list_active",
        "recover_active",
        "commit_publication",
        "audit_integrity",
        "initialize",
    ],
)
def test_missing_pointer_register_is_corruption_not_virgin_state(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: any public boundary fabricating None/v0 for an absent register.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    publishing_run = (
        _publishing_run(repository, snapshot_id=SNAPSHOT_A)
        if operation == "commit_publication"
        else None
    )
    with sqlite3.connect(repository.database_path) as connection:
        has_register = connection.execute(
            "SELECT 1 FROM active_snapshots WHERE book_id = 'book-alpha'"
        ).fetchone()
    if has_register is not None:
        _execute_without_named_triggers(
            repository,
            ("active_pointer_delete_immutable",),
            "DELETE FROM active_snapshots WHERE book_id = 'book-alpha'",
            (),
        )

    with pytest.raises(RunDatabaseError, match="pointer"):
        if operation == "create_or_join":
            repository.create_or_join(
                _new_run(
                    run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J0A",
                    client_idempotency_key="missing-register",
                ),
                now=T1,
            )
        elif operation == "get_book_head":
            repository.get_book_head("book-alpha")
        elif operation == "get_active":
            repository.get_active("book-alpha")
        elif operation == "list_active":
            repository.list_active()
        elif operation == "recover_active":
            repository.recover_active(
                "book-alpha",
                verify=lambda _snapshot_id: pytest.fail(
                    "missing-register recovery must not invoke verification"
                ),
                now=T3,
            )
        elif operation == "commit_publication":
            assert publishing_run is not None
            repository.commit_publication(
                publishing_run.run_id,
                _publication(SNAPSHOT_A, generation=1),
                expected_version=publishing_run.version,
                now=T3,
            )
        elif operation == "audit_integrity":
            repository.audit_integrity()
        else:
            RunRepository(tmp_path).initialize()


_POINTER_BOUNDARY_OPERATIONS = (
    "create_or_join",
    "get_book_head",
    "get_active",
    "list_active",
    "recover_active",
    "commit_publication",
    "audit_integrity",
    "initialize",
)


def _exercise_pointer_boundary(
    repository: RunRepository,
    operation: str,
    *,
    publishing_run=None,
) -> None:
    if operation == "create_or_join":
        repository.create_or_join(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0A",
                client_idempotency_key="pointer-boundary",
            ),
            now=T7,
        )
    elif operation == "get_book_head":
        repository.get_book_head("book-alpha")
    elif operation == "get_active":
        repository.get_active("book-alpha")
    elif operation == "list_active":
        repository.list_active()
    elif operation == "recover_active":
        repository.recover_active(
            "book-alpha",
            verify=lambda _snapshot_id: pytest.fail(
                "corrupt pointer state must be rejected before verification"
            ),
            now=T7,
        )
    elif operation == "commit_publication":
        assert publishing_run is not None
        repository.commit_publication(
            publishing_run.run_id,
            _publication(publishing_run.candidate_snapshot_id, generation=1),
            expected_version=publishing_run.version,
            now=T7,
        )
    elif operation == "audit_integrity":
        repository.audit_integrity()
    else:
        RunRepository(repository.root).initialize()


@pytest.mark.parametrize("operation", _POINTER_BOUNDARY_OPERATIONS)
def test_pointer_register_behind_future_transition_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: a rolled-back register serving A/v1 while durable B/v2 exists,
    # then allowing an ordinary writer to reuse revision two.
    repository = _repository(tmp_path)
    snapshot_ids, _ = _seed_publication_chain(repository, 2)
    publishing_run = (
        _publishing_run_at(
            repository,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0B",
            snapshot_id=SNAPSHOT_A,
            now=T1,
        )
        if operation == "commit_publication"
        else None
    )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        """
        UPDATE active_snapshots
        SET snapshot_id = ?, book_generation = 1,
            pointer_version = 1, updated_at_utc = ?
        WHERE book_id = 'book-alpha'
        """,
        (snapshot_ids[0], "2026-08-20T08:00:00.000000Z"),
        foreign_keys=True,
    )

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        _exercise_pointer_boundary(
            repository,
            operation,
            publishing_run=publishing_run,
        )

    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshot_manifests WHERE snapshot_id = ?",
            (SNAPSHOT_A,),
        ).fetchone()[0] == 0


def test_idempotent_publication_retry_validates_pointer_tail(
    tmp_path: Path,
) -> None:
    # Break caught: a completed publisher returning stale durable truth after the
    # pointer register was rolled behind a later successful publication.
    repository = _repository(tmp_path)
    first, published_first, _, _ = _publish_two_snapshot_chain(repository)
    _roll_pointer_register_back_to_first_publication(repository)

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        repository.commit_publication(
            first.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=published_first.run.version,
            now=T7,
        )

    assert repository.get(first.run_id) == published_first.run


def test_cancelled_publication_rejection_validates_pointer_tail_before_commit(
    tmp_path: Path,
) -> None:
    # Break caught: cancellation terminalizing a publisher before discovering that
    # the active register hides a later durable pointer transition.
    repository = _repository(tmp_path)
    _publish_two_snapshot_chain(repository)
    publisher = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K1B",
        snapshot_id=SNAPSHOT_C,
        now=T5,
    )
    cancelled = repository.request_cancel(publisher.run_id, now=T6)
    _roll_pointer_register_back_to_first_publication(repository)

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        repository.commit_publication(
            publisher.run_id,
            _publication(SNAPSHOT_C, generation=1),
            expected_version=cancelled.version,
            now=T7,
        )

    assert repository.get(publisher.run_id) == cancelled


def test_stale_generation_rejection_validates_pointer_tail_before_commit(
    tmp_path: Path,
) -> None:
    # Break caught: a stale-generation rejection becoming durable before discovering
    # that the active register hides a later durable pointer transition.
    repository = _repository(tmp_path)
    _publish_two_snapshot_chain(repository)
    publisher = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K1C",
        snapshot_id=SNAPSHOT_C,
        now=T5,
    )
    repository.advance_book_head("book-alpha", 2, BOOK_REF_2, now=T6)
    _roll_pointer_register_back_to_first_publication(repository)

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        repository.commit_publication(
            publisher.run_id,
            _publication(SNAPSHOT_C, generation=1),
            expected_version=publisher.version,
            now=T7,
        )

    assert repository.get(publisher.run_id) == publisher


def test_future_transition_cannot_be_compounded_into_duplicate_epoch(
    tmp_path: Path,
) -> None:
    # Break caught: a schema-valid B/v2 witness hidden behind A/v1, followed by
    # an ordinary C publisher also committing revision two.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    candidate = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0F",
        snapshot_id=SNAPSHOT_C,
        now=T4,
    )
    raw_run_id = "run_01J5X5S8J5J8P7KQ4Y0T3N6K0G"
    fingerprint = "9" * 64
    request = _new_run(
        run_id=raw_run_id,
        request_fingerprint=fingerprint,
        client_idempotency_key=None,
    )
    identity = RunRepository._idempotency_identity(request, 1, None)
    timestamp = "2026-08-20T08:04:00.000000Z"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
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
                ?, 'ANALYTICAL_SNAPSHOT', ?, ?, NULL, 'book-alpha', 1,
                ?, 1, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                NULL, ?, ?, NULL, NULL, NULL, 1
            )
            """,
            (
                raw_run_id,
                identity,
                fingerprint,
                SNAPSHOT_A,
                "2026-08-20T08:00:00.000000Z",
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                SNAPSHOT_B,
                SNAPSHOT_B,
            ),
        )
        connection.execute(
            """
            INSERT INTO snapshot_manifests (
                snapshot_id, run_id, book_id, book_generation, snapshot_status,
                schema_version, hash_algorithm, manifest_relpath, envelope_sha256,
                envelope_byte_length, published_at_utc
            ) VALUES (?, ?, 'book-alpha', 1, 'BLESSED',
                'analytical_snapshot_manifest_v1', 'sha256', ?, ?, 4096, ?)
            """,
            (
                SNAPSHOT_B,
                raw_run_id,
                "snapshots/manifests/analytical_snapshot_manifest_v1/"
                f"{SNAPSHOT_B[:2]}/{SNAPSHOT_B}.json",
                "7" * 64,
                timestamp,
            ),
        )

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        repository.commit_publication(
            candidate.run_id,
            _publication(SNAPSHOT_C, generation=1),
            expected_version=candidate.version,
            now=T5,
        )

    assert repository.get(candidate.run_id).run_outcome is RunOutcome.RUNNING
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshot_manifests WHERE snapshot_id = ?",
            (SNAPSHOT_C,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshot_runs "
            "WHERE book_id = 'book-alpha' AND expected_active_pointer_version = 1 "
            "AND run_outcome = 'SUCCEEDED' AND published_snapshot_id IS NOT NULL"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("operation", _POINTER_BOUNDARY_OPERATIONS)
def test_pointer_tail_with_wrong_immediate_predecessor_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: C/v3 claiming historical A rather than the actual B/v2 tail.
    repository = _repository(tmp_path)
    snapshot_ids, run_ids = _seed_publication_chain(repository, 3)
    publishing_run = (
        _publishing_run_at(
            repository,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0C",
            snapshot_id=SNAPSHOT_A,
            now=T1,
        )
        if operation == "commit_publication"
        else None
    )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        "UPDATE snapshot_runs SET expected_active_snapshot_id = ? WHERE run_id = ?",
        (snapshot_ids[0], run_ids[2]),
    )

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        _exercise_pointer_boundary(
            repository,
            operation,
            publishing_run=publishing_run,
        )


@pytest.mark.parametrize("operation", _POINTER_BOUNDARY_OPERATIONS)
def test_pointer_tail_with_backward_transition_clock_fails_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: a v2 removal predating the v1 publication it removed.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda _snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )
    publishing_run = (
        _publishing_run_at(
            repository,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0D",
            snapshot_id=SNAPSHOT_B,
            now=T5,
        )
        if operation == "commit_publication"
        else None
    )
    regressed_time = "2026-08-20T08:02:00.000000Z"
    _execute_without_named_triggers(
        repository,
        _RECOVERY_EVENT_UPDATE_BYPASS,
        "UPDATE snapshot_recovery_events SET recorded_at_utc = ? "
        "WHERE book_id = 'book-alpha'",
        (regressed_time,),
    )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        "UPDATE active_snapshots SET updated_at_utc = ? "
        "WHERE book_id = 'book-alpha'",
        (regressed_time,),
    )

    with pytest.raises(RunDatabaseError, match="pointer|history"):
        _exercise_pointer_boundary(
            repository,
            operation,
            publishing_run=publishing_run,
        )


@pytest.mark.parametrize("operation", ["audit_integrity", "initialize"])
def test_sparse_huge_pointer_epoch_fails_with_bounded_memory(
    tmp_path: Path,
    operation: str,
) -> None:
    # Break caught: set(range(pointer_version)) allocating from a corrupt scalar rather
    # than from the three durable transition rows actually present.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda _snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )
    second = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6K0E",
        snapshot_id=SNAPSHOT_B,
        now=T5,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T5,
    )
    huge_version = 100_000
    _execute_without_named_triggers(
        repository,
        _RECOVERY_EVENT_UPDATE_BYPASS,
        "UPDATE snapshot_recovery_events SET expected_pointer_version = ? "
        "WHERE book_id = 'book-alpha'",
        (huge_version - 2,),
    )
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_ALLOCATION_UPDATE_BYPASS,
        "UPDATE snapshot_runs SET expected_active_pointer_version = ? WHERE run_id = ?",
        (huge_version - 1, second.run_id),
    )
    _execute_without_named_triggers(
        repository,
        _ACTIVE_POINTER_UPDATE_BYPASS,
        "UPDATE active_snapshots SET pointer_version = ? WHERE book_id = 'book-alpha'",
        (huge_version,),
    )

    tracemalloc.start()
    try:
        with pytest.raises(RunDatabaseError, match="pointer|history"):
            _exercise_pointer_boundary(repository, operation)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 6_000_000


def test_pointer_tail_and_replay_queries_use_partial_indexes_without_sorting(
    tmp_path: Path,
) -> None:
    # Break caught: a bounded guard or streaming replay silently scanning/sorting history.
    repository = _TracingVmRunRepository(tmp_path)
    repository.initialize()
    _seed_publication_chain(repository, 3)
    repository.statements.clear()

    repository.audit_integrity()

    statements = [" ".join(statement.split()) for statement in repository.statements]
    future_queries = [
        statement
        for statement in statements
        if "expected_active_pointer_version >=" in statement
        and "expected_pointer_version >=" in statement
    ]
    replay_queries = [
        statement
        for statement in statements
        if "UNION ALL" in statement and "ORDER BY _sort_version" in statement
    ]
    assert len(future_queries) >= 1
    assert len(replay_queries) == 1

    with sqlite3.connect(repository.database_path) as connection:
        for statement in (future_queries[0], replay_queries[0]):
            plan = " ".join(
                row[3]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")
            )
            assert "snapshot_runs_by_book_pointer_version" in plan
            assert "recovery_events_by_book_pointer_version" in plan
            assert "TEMP B-TREE" not in plan


def test_removed_pointer_rejects_rolled_back_allocation_and_reopens(
    tmp_path: Path,
) -> None:
    # Break caught: deleting the active row erasing the T4 pointer clock, so a T1
    # request is admitted as though the book had never had an active snapshot.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )

    assert repository.get_active("book-alpha") is None
    with pytest.raises(ValueError, match="pointer"):
        repository.create_or_join(
            _new_run(
                run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J1A",
                client_idempotency_key="after-removal-rollback",
            ),
            now=T1,
        )

    repository.audit_integrity()
    reopened = RunRepository(tmp_path)
    reopened.initialize()
    assert reopened.get_active("book-alpha") is None


def test_pointer_epoch_never_resets_across_remove_publish_cycles(
    tmp_path: Path,
) -> None:
    # Break caught: each removal making the next publication recreate pointer version one.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )

    second = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J1B",
            client_idempotency_key="after-removal-b",
        ),
        now=T5,
    ).record
    assert second.expected_active_snapshot_id is None
    assert second.expected_active_pointer_version == 2
    second = _publishing_run_at(
        repository,
        run_id=second.run_id,
        snapshot_id=SNAPSHOT_B,
        now=T5,
        record=second,
    )
    published_b = repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T5,
    )
    assert published_b.active is not None
    assert published_b.active.pointer_version == 3

    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T6,
    )
    third = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J1C",
            client_idempotency_key="after-removal-c",
        ),
        now=T7,
    ).record
    assert third.expected_active_snapshot_id is None
    assert third.expected_active_pointer_version == 4
    third = _publishing_run_at(
        repository,
        run_id=third.run_id,
        snapshot_id=SNAPSHOT_C,
        now=T7,
        record=third,
    )
    published_c = repository.commit_publication(
        third.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=third.version,
        now=T7,
    )

    assert published_c.active is not None
    assert published_c.active.pointer_version == 5
    repository.audit_integrity()
    reopened = RunRepository(tmp_path)
    reopened.initialize()
    assert reopened.get_active("book-alpha") == published_c.active


def test_stale_active_after_removal_inherits_tombstone_clock(
    tmp_path: Path,
) -> None:
    # Break caught: a stale publisher recording T4 even though removal at T5 caused
    # the lost pointer CAS.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    stale = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J2A",
        snapshot_id=SNAPSHOT_B,
        now=T4,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T5,
    )

    rejected = repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=stale.version,
        now=T3,
    )

    assert rejected.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert rejected.run.finished_at_utc == T5
    assert rejected.run.updated_at_utc == T5
    assert repository.get_active("book-alpha") is None
    later = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J2C",
        snapshot_id=SNAPSHOT_C,
        now=T6,
    )
    repository.commit_publication(
        later.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=later.version,
        now=T6,
    )
    repository.audit_integrity()
    RunRepository(tmp_path).initialize()


def test_stale_active_removal_clock_corruption_fails_all_audit_boundaries(
    tmp_path: Path,
) -> None:
    # Break caught: coherent scalar timestamps hiding stale-pointer evidence that
    # predates the durable removal which caused it.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    stale = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J2B",
        snapshot_id=SNAPSHOT_B,
        now=T4,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T5,
    )
    repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=stale.version,
        now=T5,
    )
    timestamp = "2026-08-20T08:04:00.000000Z"
    _execute_without_named_triggers(
        repository,
        _TERMINAL_RUN_UPDATE_BYPASS,
        "UPDATE snapshot_runs SET finished_at_utc = ?, updated_at_utc = ? "
        "WHERE run_id = ?",
        (timestamp, timestamp, stale.run_id),
    )

    with pytest.raises(RunDatabaseError, match="pointer"):
        repository.get(stale.run_id)
    with pytest.raises(RunDatabaseError, match="pointer"):
        repository.audit_integrity()
    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()


def test_recovery_cas_lost_uses_newer_active_pointer_clock(
    tmp_path: Path,
) -> None:
    # Break caught: CAS_LOST evidence persisted at caller T4 after another publisher
    # durably advanced the pointer at T5.
    armed = False
    candidate = None

    def publish_new_active(stage: str) -> None:
        if armed and stage == "recovery.after_selection":
            assert candidate is not None
            RunRepository(tmp_path).commit_publication(
                candidate.run_id,
                _publication(SNAPSHOT_C, generation=1),
                expected_version=candidate.version,
                now=T5,
            )

    repository = RunRepository(tmp_path, fault_injector=publish_new_active)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T2,
    )
    second = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J3A",
        snapshot_id=SNAPSHOT_B,
        now=T3,
    )
    repository.commit_publication(
        second.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=second.version,
        now=T3,
    )
    candidate = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J3B",
        snapshot_id=SNAPSHOT_C,
        now=T4,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    armed = True
    recovered = repository.recover_active("book-alpha", verify=verify, now=T4)

    assert recovered.decision is ActiveRecoveryDecision.CAS_LOST
    assert recovered.active is not None
    assert recovered.active.snapshot_id == SNAPSHOT_C
    assert recovered.active.updated_at_utc == T5
    assert recovered.event is not None
    assert recovered.event.recorded_at_utc == T5
    repository.audit_integrity()
    RunRepository(tmp_path).initialize()


@pytest.mark.parametrize("resolution", ["fallback", "removal"])
def test_recovery_clock_rollback_clamps_mutation_and_evidence(
    tmp_path: Path,
    resolution: str,
) -> None:
    # Break caught: a caller clock older than the active pointer preventing recovery or
    # moving the replacement/removal transition backward.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    if resolution == "fallback":
        first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
        repository.commit_publication(
            first.run_id,
            _publication(SNAPSHOT_A, generation=1),
            expected_version=first.version,
            now=T3,
        )
    current = _publishing_run_at(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J3C",
        snapshot_id=SNAPSHOT_B,
        now=T5,
    )
    repository.commit_publication(
        current.run_id,
        _publication(SNAPSHOT_B, generation=1),
        expected_version=current.version,
        now=T5,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B or resolution == "removal":
            raise ValueError("corrupt")
        return _verified(snapshot_id)

    recovered = repository.recover_active("book-alpha", verify=verify, now=T4)

    assert recovered.decision is (
        ActiveRecoveryDecision.REPOINTED
        if resolution == "fallback"
        else ActiveRecoveryDecision.REMOVED
    )
    assert recovered.event is not None
    assert recovered.event.recorded_at_utc == T5
    assert recovered.active is None or recovered.active.updated_at_utc == T5
    repository.audit_integrity()


def test_recovery_on_tombstone_is_noop_without_verification_or_event(
    tmp_path: Path,
) -> None:
    # Protective regression: a private tombstone must still look absent to recovery callers.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T4,
    )
    calls = 0

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        nonlocal calls
        calls += 1
        return _verified(snapshot_id)

    recovered = repository.recover_active("book-alpha", verify=verify, now=T5)

    assert recovered.decision is ActiveRecoveryDecision.UNCHANGED
    assert recovered.previous_active is None
    assert recovered.active is None
    assert recovered.event is None
    assert calls == 0
    assert len(repository.list_recovery_events("book-alpha")) == 1


def test_losing_recovery_inherits_concurrent_removal_epoch_and_clock(
    tmp_path: Path,
) -> None:
    # Break caught: R1 appending sequence two at T5 after R2 durably removed the
    # same pointer and appended sequence one at T6.
    armed = False

    def remove_concurrently(stage: str) -> None:
        nonlocal armed
        if armed and stage == "recovery.after_selection":
            armed = False
            other = RunRepository(tmp_path)
            removed = other.recover_active(
                "book-alpha",
                verify=lambda snapshot_id: (_ for _ in ()).throw(
                    ValueError("corrupt")
                ),
                now=T6,
            )
            assert removed.decision is ActiveRecoveryDecision.REMOVED

    repository = RunRepository(tmp_path, fault_injector=remove_concurrently)
    repository.initialize()
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )

    armed = True
    losing = repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T5,
    )

    assert losing.decision is ActiveRecoveryDecision.CAS_LOST
    assert losing.active is None
    assert repository.get_active("book-alpha") is None
    events = repository.list_recovery_events("book-alpha")
    assert [event.resolution_action for event in events] == [
        ActiveRecoveryDecision.REMOVED,
        ActiveRecoveryDecision.CAS_LOST,
    ]
    assert [event.recorded_at_utc for event in events] == [T6, T6]
    next_run = repository.create_or_join(
        _new_run(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3N6J4A",
            client_idempotency_key="after-concurrent-removal",
        ),
        now=T7,
    ).record
    assert next_run.expected_active_snapshot_id is None
    assert next_run.expected_active_pointer_version == 2
    repository.audit_integrity()
    reopened = RunRepository(tmp_path)
    reopened.initialize()
    assert reopened.get_active("book-alpha") is None


def test_recovery_event_clock_regression_after_removal_fails_audit(
    tmp_path: Path,
) -> None:
    # Break caught: append-only sequence order accepting a later CAS_LOST event whose
    # clock predates the earlier removal which defeated it.
    repository = _repository(tmp_path)
    repository.advance_book_head("book-alpha", 1, BOOK_REF_1, now=T0)
    first = _publishing_run(repository, snapshot_id=SNAPSHOT_A)
    repository.commit_publication(
        first.run_id,
        _publication(SNAPSHOT_A, generation=1),
        expected_version=first.version,
        now=T3,
    )
    repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T6,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_raw_recovery_event(
            connection,
            rejected_snapshot_id=SNAPSHOT_A,
            expected_pointer_version=1,
            recorded_at_utc="2026-08-20T08:06:00.000000Z",
        )
    _execute_without_named_triggers(
        repository,
        _RECOVERY_EVENT_UPDATE_BYPASS,
        "UPDATE snapshot_recovery_events SET recorded_at_utc = ? "
        "WHERE event_sequence = 2",
        ("2026-08-20T08:05:00.000000Z",),
    )

    with pytest.raises(RunDatabaseError, match="recovery"):
        repository.list_recovery_events("book-alpha")
    with pytest.raises(RunDatabaseError, match="recovery"):
        repository.audit_integrity()
    with pytest.raises(RunDatabaseError):
        RunRepository(tmp_path).initialize()
