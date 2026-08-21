CREATE TABLE book_heads (
    book_id TEXT NOT NULL PRIMARY KEY CHECK (length(book_id) BETWEEN 1 AND 256),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    canonical_book_ref TEXT NOT NULL CHECK (
        length(canonical_book_ref) = 64
        AND canonical_book_ref NOT GLOB '*[^0-9a-f]*'
    ),
    updated_at_utc TEXT NOT NULL CHECK (
        length(updated_at_utc) = 27
        AND updated_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    version INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE snapshot_runs (
    run_id TEXT NOT NULL PRIMARY KEY CHECK (length(run_id) BETWEEN 16 AND 128),
    run_kind TEXT NOT NULL CHECK (
        length(CAST(run_kind AS BLOB)) BETWEEN 1 AND 64
    ),
    idempotency_identity TEXT NOT NULL CHECK (
        length(idempotency_identity) = 64
        AND idempotency_identity NOT GLOB '*[^0-9a-f]*'
    ),
    request_fingerprint TEXT NOT NULL CHECK (
        length(request_fingerprint) = 64
        AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    client_idempotency_key_digest TEXT CHECK (
        client_idempotency_key_digest IS NULL
        OR (
            length(client_idempotency_key_digest) = 64
            AND client_idempotency_key_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    book_id TEXT REFERENCES book_heads(book_id),
    captured_generation INTEGER CHECK (captured_generation >= 0),
    expected_active_snapshot_id TEXT CHECK (
        expected_active_snapshot_id IS NULL
        OR (
            length(expected_active_snapshot_id) = 64
            AND expected_active_snapshot_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    expected_active_pointer_version INTEGER NOT NULL CHECK (
        expected_active_pointer_version >= 0
    ),
    target_cut_utc TEXT CHECK (
        target_cut_utc IS NULL
        OR (
            length(target_cut_utc) = 27
            AND target_cut_utc GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    ),
    requested_at_utc TEXT NOT NULL CHECK (
        length(requested_at_utc) = 27
        AND requested_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    started_at_utc TEXT CHECK (
        started_at_utc IS NULL
        OR (
            length(started_at_utc) = 27
            AND started_at_utc GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    ),
    updated_at_utc TEXT NOT NULL CHECK (
        length(updated_at_utc) = 27
        AND updated_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    finished_at_utc TEXT CHECK (
        finished_at_utc IS NULL
        OR (
            length(finished_at_utc) = 27
            AND finished_at_utc GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    ),
    run_stage TEXT NOT NULL CHECK (run_stage IN (
        'QUEUED', 'INGESTING', 'RECONCILING', 'VALIDATING', 'MODELING', 'PUBLISHING'
    )),
    run_outcome TEXT NOT NULL CHECK (run_outcome IN (
        'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
    )),
    cancel_requested_at_utc TEXT CHECK (
        cancel_requested_at_utc IS NULL
        OR (
            length(cancel_requested_at_utc) = 27
            AND cancel_requested_at_utc GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    ),
    candidate_snapshot_id TEXT CHECK (
        candidate_snapshot_id IS NULL
        OR (
            length(candidate_snapshot_id) = 64
            AND candidate_snapshot_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    published_snapshot_id TEXT CHECK (
        published_snapshot_id IS NULL
        OR (
            length(published_snapshot_id) = 64
            AND published_snapshot_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    result_json TEXT CHECK (
        result_json IS NULL
        OR (
            length(CAST(result_json AS BLOB)) <= 1024
            AND json_valid(result_json)
            AND json_extract(result_json, '$.schema_version') = 'durable_run_result_v1'
            AND json_extract(result_json, '$.result_code') IN (
                'EMPTY', 'BOOLEAN', 'INTEGER', 'SYNC_COMPLETED', 'ARTIFACT_REFERENCE'
            )
        )
    ),
    error_code TEXT CHECK (
        error_code IS NULL OR error_code IN (
            'SUBMISSION_FAILED', 'WORKER_FAILED', 'SERIALIZATION_FAILED',
            'BROKEN_PROCESS_POOL', 'DISK_WRITE_FAILED', 'DATABASE_FAILED',
            'CANCELLED_BY_USER', 'INTERRUPTED', 'STALE_BOOK_GENERATION',
            'STALE_ACTIVE_POINTER', 'HARD_GATE_FAILED', 'SHUTDOWN_INTERRUPTED'
        )
    ),
    error_message TEXT CHECK (
        (error_code IS NULL AND error_message IS NULL)
        OR (error_code = 'SUBMISSION_FAILED' AND error_message = 'executor submission failed')
        OR (error_code = 'WORKER_FAILED' AND error_message = 'worker execution failed')
        OR (error_code = 'SERIALIZATION_FAILED' AND error_message = 'result serialization failed')
        OR (error_code = 'BROKEN_PROCESS_POOL' AND error_message = 'worker pool unavailable')
        OR (error_code = 'DISK_WRITE_FAILED' AND error_message = 'durable artifact write failed')
        OR (error_code = 'DATABASE_FAILED' AND error_message = 'durable catalog operation failed')
        OR (error_code = 'CANCELLED_BY_USER' AND error_message = 'cancelled by user')
        OR (error_code = 'INTERRUPTED' AND error_message = 'run interrupted by process restart')
        OR (error_code = 'STALE_BOOK_GENERATION' AND error_message = 'canonical book generation changed before publication')
        OR (error_code = 'STALE_ACTIVE_POINTER' AND error_message = 'active snapshot pointer changed before publication')
        OR (error_code = 'HARD_GATE_FAILED' AND error_message = 'analytical hard gate failed')
        OR (error_code = 'SHUTDOWN_INTERRUPTED' AND error_message = 'run interrupted by shutdown')
    ),
    version INTEGER NOT NULL CHECK (version >= 1),
    CHECK (
        (book_id IS NULL AND captured_generation IS NULL AND target_cut_utc IS NULL)
        OR (book_id IS NOT NULL AND captured_generation IS NOT NULL AND target_cut_utc IS NOT NULL)
    ),
    CHECK (
        (book_id IS NULL AND expected_active_snapshot_id IS NULL
            AND expected_active_pointer_version = 0)
        OR (
            book_id IS NOT NULL
            AND (
                (expected_active_snapshot_id IS NULL
                    AND expected_active_pointer_version = 0)
                OR (expected_active_snapshot_id IS NOT NULL
                    AND expected_active_pointer_version >= 1)
            )
        )
    ),
    CHECK (updated_at_utc >= requested_at_utc),
    CHECK (started_at_utc IS NULL OR (
        started_at_utc >= requested_at_utc AND started_at_utc <= updated_at_utc
    )),
    CHECK (cancel_requested_at_utc IS NULL OR (
        cancel_requested_at_utc >= requested_at_utc
        AND cancel_requested_at_utc <= updated_at_utc
    )),
    CHECK (finished_at_utc IS NULL OR (
        finished_at_utc >= requested_at_utc AND finished_at_utc <= updated_at_utc
        AND (started_at_utc IS NULL OR finished_at_utc >= started_at_utc)
        AND (
            cancel_requested_at_utc IS NULL
            OR finished_at_utc >= cancel_requested_at_utc
        )
    )),
    CHECK (
        (run_outcome = 'RUNNING' AND finished_at_utc IS NULL)
        OR (run_outcome <> 'RUNNING' AND finished_at_utc IS NOT NULL)
    ),
    CHECK (
        (run_outcome IN ('RUNNING', 'SUCCEEDED') AND error_code IS NULL)
        OR (run_outcome = 'FAILED' AND error_code IS NOT NULL AND error_code <> 'CANCELLED_BY_USER')
        OR (
            run_outcome = 'CANCELLED'
            AND error_code = 'CANCELLED_BY_USER'
            AND cancel_requested_at_utc IS NOT NULL
        )
    ),
    CHECK (result_json IS NULL OR (run_outcome = 'SUCCEEDED' AND book_id IS NULL)),
    CHECK (published_snapshot_id IS NULL OR run_outcome = 'SUCCEEDED'),
    CHECK (
        book_id IS NOT NULL OR (
            candidate_snapshot_id IS NULL AND published_snapshot_id IS NULL
        )
    ),
    CHECK (
        run_outcome <> 'SUCCEEDED' OR book_id IS NULL OR (
            run_stage = 'PUBLISHING'
            AND candidate_snapshot_id IS NOT NULL
            AND published_snapshot_id IS NOT NULL
            AND published_snapshot_id = candidate_snapshot_id
        )
    ),
    FOREIGN KEY (book_id, expected_active_snapshot_id)
        REFERENCES snapshot_manifests(book_id, snapshot_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX one_live_idempotency_identity
    ON snapshot_runs(run_kind, idempotency_identity)
    WHERE run_outcome = 'RUNNING';

CREATE UNIQUE INDEX one_live_snapshot_per_book_generation
    ON snapshot_runs(book_id, captured_generation)
    WHERE run_outcome = 'RUNNING' AND book_id IS NOT NULL;

CREATE INDEX snapshot_runs_by_identity
    ON snapshot_runs(run_kind, idempotency_identity);

CREATE INDEX snapshot_runs_by_preimage
    ON snapshot_runs(
        run_kind,
        request_fingerprint,
        client_idempotency_key_digest,
        book_id,
        captured_generation,
        target_cut_utc
    );

CREATE INDEX snapshot_runs_by_book_generation
    ON snapshot_runs(book_id, captured_generation);

CREATE INDEX snapshot_runs_by_book_requested
    ON snapshot_runs(book_id, requested_at_utc DESC, run_id DESC);

CREATE TRIGGER snapshot_run_identity_collision_on_insert
BEFORE INSERT ON snapshot_runs
WHEN EXISTS (
    SELECT 1 FROM snapshot_runs AS existing
    WHERE existing.run_id = NEW.run_id
       OR (
            NEW.run_outcome = 'RUNNING'
            AND existing.run_outcome = 'RUNNING'
            AND existing.run_kind = NEW.run_kind
            AND existing.idempotency_identity = NEW.idempotency_identity
       )
       OR (
            NEW.run_outcome = 'RUNNING'
            AND NEW.book_id IS NOT NULL
            AND existing.run_outcome = 'RUNNING'
            AND existing.book_id = NEW.book_id
            AND existing.captured_generation = NEW.captured_generation
       )
)
BEGIN
    SELECT RAISE(ABORT, 'run identity is immutable');
END;

CREATE TRIGGER snapshot_run_allocation_immutable
BEFORE UPDATE OF
    run_id,
    run_kind,
    idempotency_identity,
    request_fingerprint,
    client_idempotency_key_digest,
    book_id,
    captured_generation,
    expected_active_snapshot_id,
    expected_active_pointer_version,
    target_cut_utc,
    requested_at_utc
ON snapshot_runs
WHEN NEW.run_id IS NOT OLD.run_id
  OR NEW.run_kind IS NOT OLD.run_kind
  OR NEW.idempotency_identity IS NOT OLD.idempotency_identity
  OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
  OR NEW.client_idempotency_key_digest IS NOT OLD.client_idempotency_key_digest
  OR NEW.book_id IS NOT OLD.book_id
  OR NEW.captured_generation IS NOT OLD.captured_generation
  OR NEW.expected_active_snapshot_id IS NOT OLD.expected_active_snapshot_id
  OR NEW.expected_active_pointer_version IS NOT OLD.expected_active_pointer_version
  OR NEW.target_cut_utc IS NOT OLD.target_cut_utc
  OR NEW.requested_at_utc IS NOT OLD.requested_at_utc
BEGIN
    SELECT RAISE(ABORT, 'run allocation fields are immutable');
END;

CREATE TRIGGER snapshot_run_terminal_update_immutable
BEFORE UPDATE ON snapshot_runs
WHEN OLD.run_outcome <> 'RUNNING'
BEGIN
    SELECT RAISE(ABORT, 'terminal run is immutable');
END;

CREATE TRIGGER snapshot_run_delete_immutable
BEFORE DELETE ON snapshot_runs
BEGIN
    SELECT RAISE(ABORT, 'run history is immutable');
END;

CREATE TABLE snapshot_manifests (
    publication_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (
        publication_sequence > 0
    ),
    snapshot_id TEXT NOT NULL UNIQUE CHECK (
        length(snapshot_id) = 64 AND snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL UNIQUE REFERENCES snapshot_runs(run_id),
    book_id TEXT NOT NULL REFERENCES book_heads(book_id),
    book_generation INTEGER NOT NULL CHECK (book_generation >= 0),
    snapshot_status TEXT NOT NULL CHECK (snapshot_status IN ('BLESSED', 'DEGRADED')),
    schema_version TEXT NOT NULL CHECK (
        schema_version = 'analytical_snapshot_manifest_v1'
    ),
    hash_algorithm TEXT NOT NULL CHECK (hash_algorithm = 'sha256'),
    manifest_relpath TEXT NOT NULL CHECK (
        length(manifest_relpath) <= 512
        AND manifest_relpath =
            'snapshots/manifests/analytical_snapshot_manifest_v1/'
            || substr(snapshot_id, 1, 2) || '/' || snapshot_id || '.json'
    ),
    envelope_sha256 TEXT NOT NULL CHECK (
        length(envelope_sha256) = 64 AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    envelope_byte_length INTEGER NOT NULL CHECK (envelope_byte_length >= 0),
    published_at_utc TEXT NOT NULL CHECK (
        length(published_at_utc) = 27
        AND published_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    UNIQUE (book_id, snapshot_id),
    UNIQUE (book_id, snapshot_id, book_generation)
);

CREATE INDEX blessed_manifest_fallback
    ON snapshot_manifests(book_id, publication_sequence DESC)
    WHERE snapshot_status = 'BLESSED';

CREATE INDEX snapshot_manifests_by_book_generation
    ON snapshot_manifests(book_id, book_generation);

CREATE INDEX snapshot_manifests_by_book_sequence
    ON snapshot_manifests(book_id, publication_sequence DESC);

CREATE TRIGGER manifest_identity_collision_on_insert
BEFORE INSERT ON snapshot_manifests
WHEN EXISTS (
    SELECT 1 FROM snapshot_manifests
    WHERE (
            NEW.publication_sequence > 0
            AND publication_sequence >= NEW.publication_sequence
        )
       OR snapshot_id = NEW.snapshot_id
       OR run_id = NEW.run_id
       OR (book_id = NEW.book_id AND snapshot_id = NEW.snapshot_id)
)
BEGIN
    SELECT RAISE(ABORT, 'manifest identity is immutable');
END;

CREATE TRIGGER manifest_update_immutable
BEFORE UPDATE ON snapshot_manifests
BEGIN
    SELECT RAISE(ABORT, 'manifest is immutable');
END;

CREATE TRIGGER manifest_delete_immutable
BEFORE DELETE ON snapshot_manifests
BEGIN
    SELECT RAISE(ABORT, 'manifest is immutable');
END;

CREATE TABLE active_snapshots (
    book_id TEXT NOT NULL PRIMARY KEY REFERENCES book_heads(book_id),
    snapshot_id TEXT NOT NULL,
    book_generation INTEGER NOT NULL CHECK (book_generation >= 0),
    pointer_version INTEGER NOT NULL CHECK (pointer_version >= 1),
    updated_at_utc TEXT NOT NULL CHECK (
        length(updated_at_utc) = 27
        AND updated_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    FOREIGN KEY (book_id, snapshot_id, book_generation)
        REFERENCES snapshot_manifests(book_id, snapshot_id, book_generation)
);

CREATE TABLE snapshot_recovery_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (event_sequence > 0),
    book_id TEXT NOT NULL REFERENCES book_heads(book_id),
    rejected_snapshot_id TEXT NOT NULL CHECK (
        length(rejected_snapshot_id) = 64
        AND rejected_snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    expected_pointer_version INTEGER NOT NULL CHECK (expected_pointer_version >= 1),
    resolution_action TEXT NOT NULL CHECK (
        resolution_action IN ('REPOINTED', 'REMOVED', 'CAS_LOST')
    ),
    selected_snapshot_id TEXT,
    detail_json TEXT NOT NULL CHECK (
        length(CAST(detail_json AS BLOB)) <= 65536
        AND json_valid(detail_json)
        AND json_type(detail_json, '$.failures') = 'array'
        AND json_type(detail_json, '$.omitted_count') = 'integer'
    ),
    recorded_at_utc TEXT NOT NULL CHECK (
        length(recorded_at_utc) = 27
        AND recorded_at_utc GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
    ),
    CHECK (
        (resolution_action = 'REPOINTED' AND selected_snapshot_id IS NOT NULL
            AND selected_snapshot_id <> rejected_snapshot_id)
        OR (resolution_action = 'REMOVED' AND selected_snapshot_id IS NULL)
        OR (
            resolution_action = 'CAS_LOST'
            AND (
                selected_snapshot_id IS NULL
                OR selected_snapshot_id <> rejected_snapshot_id
            )
        )
    ),
    FOREIGN KEY (book_id, rejected_snapshot_id)
        REFERENCES snapshot_manifests(book_id, snapshot_id),
    FOREIGN KEY (book_id, selected_snapshot_id)
        REFERENCES snapshot_manifests(book_id, snapshot_id)
);

CREATE INDEX recovery_events_by_book_sequence
    ON snapshot_recovery_events(book_id, event_sequence);

CREATE TRIGGER recovery_event_identity_collision_on_insert
BEFORE INSERT ON snapshot_recovery_events
WHEN NEW.event_sequence > 0
 AND EXISTS (
    SELECT 1 FROM snapshot_recovery_events
    WHERE event_sequence >= NEW.event_sequence
 )
BEGIN
    SELECT RAISE(ABORT, 'recovery event identity is immutable');
END;

CREATE TRIGGER recovery_event_time_not_before_publications
BEFORE INSERT ON snapshot_recovery_events
WHEN EXISTS (
    SELECT 1 FROM snapshot_manifests
    WHERE book_id = NEW.book_id
      AND snapshot_id = NEW.rejected_snapshot_id
      AND published_at_utc > NEW.recorded_at_utc
 )
 OR (
    NEW.selected_snapshot_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM snapshot_manifests
        WHERE book_id = NEW.book_id
          AND snapshot_id = NEW.selected_snapshot_id
          AND published_at_utc > NEW.recorded_at_utc
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'recovery event predates its publication');
END;

CREATE TRIGGER recovery_event_update_immutable
BEFORE UPDATE ON snapshot_recovery_events
BEGIN
    SELECT RAISE(ABORT, 'recovery event is immutable');
END;

CREATE TRIGGER recovery_event_delete_immutable
BEFORE DELETE ON snapshot_recovery_events
BEGIN
    SELECT RAISE(ABORT, 'recovery event is immutable');
END;

CREATE TRIGGER recovery_selected_snapshot_blessed_on_insert
BEFORE INSERT ON snapshot_recovery_events
WHEN NEW.selected_snapshot_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM snapshot_manifests
    WHERE book_id = NEW.book_id
      AND snapshot_id = NEW.selected_snapshot_id
      AND snapshot_status = 'BLESSED'
 )
BEGIN
    SELECT RAISE(ABORT, 'selected recovery snapshot must be BLESSED');
END;

CREATE TRIGGER recovery_selected_snapshot_blessed_on_update
BEFORE UPDATE OF book_id, selected_snapshot_id ON snapshot_recovery_events
WHEN NEW.selected_snapshot_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM snapshot_manifests
    WHERE book_id = NEW.book_id
      AND snapshot_id = NEW.selected_snapshot_id
      AND snapshot_status = 'BLESSED'
 )
BEGIN
    SELECT RAISE(ABORT, 'selected recovery snapshot must be BLESSED');
END;

CREATE TRIGGER manifest_selected_snapshot_stays_blessed
BEFORE UPDATE OF snapshot_status ON snapshot_manifests
WHEN OLD.snapshot_status = 'BLESSED'
 AND NEW.snapshot_status <> 'BLESSED'
 AND EXISTS (
    SELECT 1 FROM snapshot_recovery_events
    WHERE book_id = OLD.book_id
      AND selected_snapshot_id = OLD.snapshot_id
 )
BEGIN
    SELECT RAISE(ABORT, 'selected recovery snapshot must remain BLESSED');
END;

PRAGMA user_version = 1;
