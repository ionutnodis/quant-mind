CREATE TABLE book_heads (
    book_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    canonical_book_ref TEXT NOT NULL CHECK (
        length(canonical_book_ref) = 64
        AND canonical_book_ref NOT GLOB '*[^0-9a-f]*'
    ),
    updated_at_utc TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE snapshot_runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) BETWEEN 16 AND 128),
    run_kind TEXT NOT NULL CHECK (length(run_kind) BETWEEN 1 AND 64),
    idempotency_identity TEXT NOT NULL CHECK (
        length(idempotency_identity) = 64
        AND idempotency_identity NOT GLOB '*[^0-9a-f]*'
    ),
    request_fingerprint TEXT NOT NULL CHECK (
        length(request_fingerprint) = 64
        AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    client_idempotency_key TEXT,
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
    target_cut_utc TEXT,
    requested_at_utc TEXT NOT NULL,
    started_at_utc TEXT,
    updated_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    run_stage TEXT NOT NULL CHECK (run_stage IN (
        'QUEUED', 'INGESTING', 'RECONCILING', 'VALIDATING', 'MODELING', 'PUBLISHING'
    )),
    run_outcome TEXT NOT NULL CHECK (run_outcome IN (
        'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
    )),
    cancel_requested_at_utc TEXT,
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
        result_json IS NULL OR length(CAST(result_json AS BLOB)) <= 65536
    ),
    error_code TEXT,
    error_message TEXT CHECK (
        error_message IS NULL OR length(CAST(error_message AS BLOB)) <= 1024
    ),
    version INTEGER NOT NULL CHECK (version >= 1),
    CHECK (
        (book_id IS NULL AND captured_generation IS NULL AND target_cut_utc IS NULL)
        OR (book_id IS NOT NULL AND captured_generation IS NOT NULL AND target_cut_utc IS NOT NULL)
    ),
    CHECK (
        (expected_active_snapshot_id IS NULL AND expected_active_pointer_version = 0)
        OR (expected_active_snapshot_id IS NOT NULL AND expected_active_pointer_version >= 1)
    ),
    CHECK (
        (run_outcome = 'RUNNING' AND finished_at_utc IS NULL)
        OR (run_outcome <> 'RUNNING' AND finished_at_utc IS NOT NULL)
    ),
    CHECK (run_outcome <> 'FAILED' OR error_code IS NOT NULL),
    CHECK (run_outcome <> 'CANCELLED' OR error_code = 'CANCELLED_BY_USER'),
    CHECK (published_snapshot_id IS NULL OR run_outcome = 'SUCCEEDED')
);

CREATE UNIQUE INDEX one_live_idempotency_identity
    ON snapshot_runs(run_kind, idempotency_identity)
    WHERE run_outcome = 'RUNNING';

CREATE UNIQUE INDEX one_live_snapshot_per_book_generation
    ON snapshot_runs(book_id, captured_generation)
    WHERE run_outcome = 'RUNNING' AND book_id IS NOT NULL;

CREATE INDEX snapshot_runs_by_book_requested
    ON snapshot_runs(book_id, requested_at_utc DESC, run_id DESC);

CREATE TABLE snapshot_manifests (
    publication_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL UNIQUE CHECK (
        length(snapshot_id) = 64 AND snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL UNIQUE REFERENCES snapshot_runs(run_id),
    book_id TEXT NOT NULL REFERENCES book_heads(book_id),
    book_generation INTEGER NOT NULL CHECK (book_generation >= 0),
    snapshot_status TEXT NOT NULL CHECK (snapshot_status IN ('BLESSED', 'DEGRADED')),
    schema_version TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL CHECK (hash_algorithm = 'sha256'),
    manifest_relpath TEXT NOT NULL CHECK (
        length(manifest_relpath) BETWEEN 1 AND 512
        AND substr(manifest_relpath, 1, 1) <> '/'
    ),
    envelope_sha256 TEXT NOT NULL CHECK (
        length(envelope_sha256) = 64 AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    envelope_byte_length INTEGER NOT NULL CHECK (envelope_byte_length >= 0),
    published_at_utc TEXT NOT NULL
);

CREATE INDEX blessed_manifest_fallback
    ON snapshot_manifests(book_id, publication_sequence DESC)
    WHERE snapshot_status = 'BLESSED';

CREATE TABLE active_snapshots (
    book_id TEXT PRIMARY KEY REFERENCES book_heads(book_id),
    snapshot_id TEXT NOT NULL REFERENCES snapshot_manifests(snapshot_id),
    book_generation INTEGER NOT NULL CHECK (book_generation >= 0),
    pointer_version INTEGER NOT NULL CHECK (pointer_version >= 1),
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE snapshot_recovery_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES book_heads(book_id),
    rejected_snapshot_id TEXT NOT NULL CHECK (
        length(rejected_snapshot_id) = 64
        AND rejected_snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    expected_pointer_version INTEGER NOT NULL CHECK (expected_pointer_version >= 1),
    resolution_action TEXT NOT NULL CHECK (
        resolution_action IN ('REPOINTED', 'REMOVED', 'CAS_LOST')
    ),
    selected_snapshot_id TEXT REFERENCES snapshot_manifests(snapshot_id),
    detail_json TEXT NOT NULL CHECK (length(CAST(detail_json AS BLOB)) <= 65536),
    recorded_at_utc TEXT NOT NULL,
    CHECK (
        (resolution_action = 'REPOINTED' AND selected_snapshot_id IS NOT NULL)
        OR (resolution_action IN ('REMOVED', 'CAS_LOST'))
    )
);

PRAGMA user_version = 1;
