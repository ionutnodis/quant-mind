from __future__ import annotations

import pytest

from quantmind.datastore.locking import SyncAlreadyRunning, exclusive_sync_lock


def test_sync_lock_refuses_a_second_writer_for_the_same_data_dir(tmp_path):
    with exclusive_sync_lock(tmp_path):
        with pytest.raises(SyncAlreadyRunning, match="another QuantMind sync"):
            with exclusive_sync_lock(tmp_path):
                pytest.fail("a second writer acquired the same datastore lock")


def test_sync_lock_is_released_when_the_writer_raises(tmp_path):
    with pytest.raises(RuntimeError, match="sync failed"):
        with exclusive_sync_lock(tmp_path):
            raise RuntimeError("sync failed")

    with exclusive_sync_lock(tmp_path):
        pass


def test_sync_lock_refuses_a_symlink_lock_file(tmp_path):
    target = tmp_path / "outside.lock"
    target.write_text("do not follow")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".quantmind-sync.lock").symlink_to(target)

    with pytest.raises(OSError):
        with exclusive_sync_lock(data_dir):
            pytest.fail("a symlink lock file was followed")

    assert target.read_text() == "do not follow"
