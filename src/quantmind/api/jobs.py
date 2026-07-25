"""Minimal in-process job pattern (Phase Plan decision 1A, hardened per review):
a dict + ProcessPoolExecutor futures. TTL eviction, bounded workers, cancel,
idempotent submission. Workers receive picklable args (dataframes, not file
handles) — the pure-core rule keeps them free of store contention.

Deliberately NOT a queue system. If this ever needs Redis, something about the
product changed first.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _Job:
    future: object
    created: float
    idempotency_key: str | None = None
    finished: float | None = None


class JobManager:
    def __init__(self, max_workers: int = 2, ttl_seconds: float = 3600):
        self._pool = ProcessPoolExecutor(max_workers=max_workers)
        self._jobs: dict[str, _Job] = {}
        self._by_key: dict[str, str] = {}
        self._ttl = ttl_seconds
        self._lock = Lock()

    def submit(self, fn, *args, idempotency_key: str | None = None, **kwargs) -> str:
        with self._lock:
            if idempotency_key and idempotency_key in self._by_key:
                existing = self._by_key[idempotency_key]
                if existing in self._jobs:
                    return existing
            job_id = uuid.uuid4().hex[:12]
            future = self._pool.submit(fn, *args, **kwargs)
            self._jobs[job_id] = _Job(future=future, created=time.time(), idempotency_key=idempotency_key)
            if idempotency_key:
                self._by_key[idempotency_key] = job_id
            return job_id

    def status(self, job_id: str) -> dict:
        job = self._jobs[job_id]  # KeyError -> API 404
        f = job.future
        if f.cancelled():
            return {"state": "cancelled"}
        if not f.done():
            return {"state": "running"}
        if job.finished is None:
            job.finished = time.time()
        exc = f.exception()
        if exc is not None:
            return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
        return {"state": "done", "result": f.result()}

    def cancel(self, job_id: str) -> bool:
        return self._jobs[job_id].future.cancel()

    def evict_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            expired = [
                jid for jid, j in self._jobs.items()
                if j.future.done() and now - (j.finished or j.created) > self._ttl
            ]
            for jid in expired:
                job = self._jobs.pop(jid)
                if job.idempotency_key:
                    self._by_key.pop(job.idempotency_key, None)
        return len(expired)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
