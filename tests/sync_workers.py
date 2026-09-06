"""Picklable sync test runners with no application imports.

Spawned workers import this module, not the API test module and its scientific
stack. The HTTP routes and JobManager still use the real process executor.
"""

import time
from pathlib import Path


def fast_ok() -> str:
    return "synced 3 symbols"


def fast_fail() -> str:
    raise RuntimeError("sync_cli exited 1: boom")


def gated_sync(started: Path, release: Path) -> str:
    """Stay active until the parent has exercised the running-job contract."""
    started.touch()
    deadline = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("test did not release the sync worker")
        time.sleep(0.005)
    return fast_ok()
