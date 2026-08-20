from __future__ import annotations

from pathlib import Path

import pytest

from quantmind.snapshots.store import SnapshotDurabilityError, SnapshotStore


def test_verified_existing_target_retry_must_complete_the_directory_barrier(
    tmp_path, monkeypatch
):
    payload = b"durable retry payload"
    failed_first_barrier = False

    def fail_after_link_once(stage: str, _path: Path) -> None:
        nonlocal failed_first_barrier
        if stage == "before_directory_fsync" and not failed_first_barrier:
            failed_first_barrier = True
            raise RuntimeError("crash before first directory barrier")

    first = SnapshotStore(tmp_path, fault_injector=fail_after_link_once)
    with pytest.raises(RuntimeError, match="first directory barrier"):
        first.put_bytes(
            payload,
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )
    assert failed_first_barrier

    retry = SnapshotStore(tmp_path)
    barrier_attempts = 0

    def fail_retry_barrier(_path: Path) -> None:
        nonlocal barrier_attempts
        barrier_attempts += 1
        raise OSError("directory fsync unavailable")

    monkeypatch.setattr(retry, "_fsync_directory", fail_retry_barrier)
    with pytest.raises(SnapshotDurabilityError, match="directory fsync"):
        retry.put_bytes(
            payload,
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )
    assert barrier_attempts == 1

    ref = SnapshotStore(tmp_path).put_bytes(
        payload,
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    assert SnapshotStore(tmp_path).read_verified_artifact(ref) == payload
