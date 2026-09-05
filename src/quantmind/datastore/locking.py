"""Cross-process writer coordination for one QuantMind data directory."""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SyncAlreadyRunning(RuntimeError):
    """Another process owns the datastore-wide sync writer lease."""


@contextmanager
def exclusive_sync_lock(data_dir: str | Path) -> Iterator[None]:
    """Hold a non-blocking OS lock for the duration of a cache sync.

    API-triggered syncs run in subprocesses and operators can also launch the
    documented CLI directly. An in-process job mutex cannot coordinate those
    writers, while ``flock`` is released by the kernel even after a crash.
    """

    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".quantmind-sync.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            try:
                owner = os.pread(fd, 64, 0).decode("ascii", errors="ignore").strip()
            except OSError:
                owner = ""
            suffix = f" (owner pid {owner})" if owner.isdigit() else ""
            raise SyncAlreadyRunning(
                f"another QuantMind sync is already writing {root}{suffix}"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
