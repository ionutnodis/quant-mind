from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantmind.snapshots.contracts import RunOutcome, RunStage, SnapshotStatus
from quantmind.snapshots.run_repository import (
    ActiveRecoveryDecision,
    GenerationRegressionError,
    IllegalRunTransitionError,
    IncompatibleLiveRunError,
    ManifestPublicationV1,
    NewRunV1,
    PublicationConflictError,
    RunDatabaseError,
    RunErrorCode,
    RunFailureV1,
    RunNotFoundError,
    RunRepository,
    StaleRunVersionError,
    TerminalRunMutationError,
)
from quantmind.snapshots.store import VerifiedSnapshotV1


T0 = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 20, 8, 1, tzinfo=UTC)
T2 = datetime(2026, 8, 20, 8, 2, tzinfo=UTC)
T3 = datetime(2026, 8, 20, 8, 3, tzinfo=UTC)
BOOK_REF_1 = "1" * 64
BOOK_REF_2 = "2" * 64
SNAPSHOT_A = "a" * 64
SNAPSHOT_B = "b" * 64
SNAPSHOT_C = "c" * 64


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
):
    record = repository.create_or_join(_new_run(run_id), now=T1).record
    record = repository.claim_start(
        record.run_id, expected_version=record.version, now=T1
    )
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        record = repository.advance_stage(
            record.run_id, stage, expected_version=record.version, now=T2
        )
    return repository.attach_candidate(
        record.run_id,
        snapshot_id,
        expected_version=record.version,
        now=T2,
    )


def _publication(
    snapshot_id: str,
    *,
    generation: int,
    status: SnapshotStatus = SnapshotStatus.BLESSED,
) -> ManifestPublicationV1:
    return ManifestPublicationV1(
        snapshot_id=snapshot_id,
        book_id="book-alpha",
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


def _verified(snapshot_id: str) -> VerifiedSnapshotV1:
    # The immutable filesystem verifier is deliberately injected into T3A. T2 owns full
    # manifest construction/validation; this typed test value represents its verified result.
    return VerifiedSnapshotV1.model_construct(
        snapshot_id=snapshot_id,
        status=SnapshotStatus.BLESSED,
        manifest=None,
    )


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
        RunFailureV1(code=RunErrorCode.WORKER_FAILED, message="worker failed"),
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
        repository.claim_start(record.run_id, expected_version=0, now=T2)


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
    # Break caught: duplicate-key, nonfinite, ambiguous, or oversized JSON entering the ledger.
    repository = _repository(tmp_path)
    created = repository.create_or_join(
        _new_run(book_id=None, client_idempotency_key="sync"), now=T0
    ).record
    result = '{"exit_code":0,"summary":"ok"}'

    completed = repository.complete_nonpublishing(
        created.run_id, result, expected_version=created.version, now=T1
    )

    assert completed.run_outcome is RunOutcome.SUCCEEDED
    assert completed.result_json == result
    for hostile in (
        '{"a":1,"a":2}',
        '{"value":NaN}',
        '{ "a": 1 }',
        json.dumps({"payload": "x" * 70_000}, separators=(",", ":")),
    ):
        fresh = repository.create_or_join(
            _new_run(
                run_id=f"run_{len(repository.list_runs()) + 1:026d}",
                book_id=None,
                client_idempotency_key=str(len(repository.list_runs())),
                request_fingerprint=f"{len(repository.list_runs()) + 1:064x}",
            ),
            now=T2,
        ).record
        with pytest.raises(ValueError):
            repository.complete_nonpublishing(
                fresh.run_id, hostile, expected_version=fresh.version, now=T3
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
        failed = repository.mark_failed(
            run.run_id,
            RunFailureV1(code=code, message=f"safe failure {index}"),
            expected_version=run.version,
            now=T1,
        )
        assert repository.get(run.run_id).error_code is code
        assert failed.error_message == f"safe failure {index}"

    for unsafe in (
        "line one\nline two",
        "/Users/alice/private/vendor-payload.json",
        "authorization: bearer secret",
        "x" * 1_025,
    ):
        with pytest.raises(ValueError):
            RunFailureV1(code=RunErrorCode.WORKER_FAILED, message=unsafe)


def test_terminal_rows_reject_every_later_mutation(tmp_path: Path) -> None:
    # Break caught: late callbacks changing immutable terminal evidence.
    repository = _repository(tmp_path)
    record = _allocated(repository)
    terminal = repository.mark_failed(
        record.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED, message="failed"),
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
            record.run_id, '{"ok":true}', expected_version=record.version, now=T1
        )
    elif outcome is RunOutcome.FAILED:
        terminal = repository.mark_failed(
            record.run_id,
            RunFailureV1(code=RunErrorCode.WORKER_FAILED, message="failed"),
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
        now=T1,
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
        now=T2,
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

    repository.recover_active("book-alpha", verify=verify, now=T2)
    rejected = repository.commit_publication(
        stale.run_id,
        _publication(SNAPSHOT_C, generation=1),
        expected_version=stale.version,
        now=T3,
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
        now=T1,
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
        now=T0,
    )

    fallbacks = repository.list_blessed_fallbacks(
        "book-alpha", excluding=SNAPSHOT_C
    )
    assert [record.snapshot_id for record in fallbacks] == [SNAPSHOT_A]
    assert [record.publication_sequence for record in repository.list_publications("book-alpha")] == [3, 2, 1]


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
        now=T1,
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
        now=T2,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt active")
        return _verified(snapshot_id)

    recovered = repository.recover_active("book-alpha", verify=verify, now=T3)

    assert recovered.decision is ActiveRecoveryDecision.REPOINTED
    assert recovered.active.snapshot_id == SNAPSHOT_A
    assert recovered.active.pointer_version == 3
    events = repository.list_recovery_events("book-alpha")
    assert len(events) == 1
    assert events[0].rejected_snapshot_id == SNAPSHOT_B
    assert events[0].selected_snapshot_id == SNAPSHOT_A
    assert json.loads(events[0].detail_json) == {
        "failures": [
            {"error_code": "ValueError", "snapshot_id": SNAPSHOT_B}
        ]
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
        now=T1,
    )

    recovered = repository.recover_active(
        "book-alpha",
        verify=lambda snapshot_id: (_ for _ in ()).throw(ValueError("corrupt")),
        now=T2,
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
        now=T1,
    )

    recovered = repository.recover_active(
        "book-alpha", verify=_verified, now=T2
    )

    assert recovered.decision is ActiveRecoveryDecision.UNCHANGED
    assert recovered.active.pointer_version == 1
    assert repository.list_recovery_events("book-alpha") == ()


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
        now=T1,
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
        now=T2,
    )

    def verify(snapshot_id: str) -> VerifiedSnapshotV1:
        if snapshot_id == SNAPSHOT_B:
            raise ValueError("corrupt")
        return _verified(snapshot_id)

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                repository.recover_active("book-alpha", verify=verify, now=T3)
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
