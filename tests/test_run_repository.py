from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import unicodedata
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


def _verified(snapshot_id: str) -> VerifiedSnapshotV1:
    return {
        SNAPSHOT_A: VERIFIED_A,
        SNAPSHOT_B: VERIFIED_B,
        SNAPSHOT_C: VERIFIED_C,
        SNAPSHOT_BETA: VERIFIED_BETA,
        SNAPSHOT_ALPHA_G2: VERIFIED_ALPHA_G2,
    }[snapshot_id]


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
    assert RunRepository(tmp_path).inspect_connection_pragmas().journal_mode == "wal"


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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE snapshot_runs SET run_kind = ? WHERE run_id = ?",
            ("R" * 65, run.run_id),
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE snapshot_manifests SET manifest_relpath = '../../outside.json'
            WHERE snapshot_id = ?
            """,
            (SNAPSHOT_A,),
        )
        connection.execute(
            """
            UPDATE snapshot_runs
            SET result_json = '{"api_key":"TOPSECRET","schema_version":"durable_run_result_v1"}'
            WHERE run_id = ?
            """,
            (result_run.run_id,),
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE snapshot_runs SET {assignments} WHERE run_id = ?",
            (run.run_id,),
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE active_snapshots SET updated_at_utc = '2026-08-20'"
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        if tamper == "foreign_book":
            connection.execute(
                """
                UPDATE active_snapshots SET snapshot_id = ?
                WHERE book_id = 'book-alpha'
                """,
                (SNAPSHOT_BETA,),
            )
        else:
            connection.execute(
                """
                UPDATE active_snapshots SET book_generation = 2
                WHERE book_id = 'book-alpha'
                """
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE snapshot_manifests SET run_id = ? WHERE snapshot_id = ?",
            (spare.run_id, SNAPSHOT_A),
        )
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
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
    with sqlite3.connect(repository.database_path) as connection:
        if tamper == "manifest_time":
            connection.execute(
                """
                UPDATE snapshot_manifests
                SET published_at_utc = '2026-08-20T08:03:01.000000Z'
                WHERE snapshot_id = ?
                """,
                (SNAPSHOT_A,),
            )
        else:
            connection.execute(
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
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
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

    with pytest.raises(RunDatabaseError):
        repository.list_recovery_events("book-alpha")


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
            with sqlite3.connect(repository.database_path) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
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
            with sqlite3.connect(repository.database_path) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "UPDATE active_snapshots SET book_generation = 2"
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
            identity = f"{index + 1:064x}"
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
                    NULL, 0, ?, ?, ?, ?, ?, 'PUBLISHING', 'SUCCEEDED',
                    NULL, ?, ?, NULL, NULL, NULL, 1
                )
                """,
                (
                    run_id,
                    identity,
                    identity,
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
        connection.execute(
            """
            INSERT INTO active_snapshots (
                book_id, snapshot_id, book_generation, pointer_version, updated_at_utc
            ) VALUES ('book-alpha', ?, 1, 1, ?)
            """,
            (snapshot_ids[-1], timestamp),
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
