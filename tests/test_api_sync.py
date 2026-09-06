"""POST /api/sync submits a job that runs sync_cli in a background worker;
GET /api/sync/{job_id} passes through JobManager status. Contract: submit
returns an id immediately; status lifecycle transitions running -> done/error;
double-submit while the first job is still tracked is idempotent (same job
id — the "double-click joins the running job" requirement); unknown id -> 404.

Only the broker-touching runner is replaced. HTTP routing, JobManager and
ProcessPoolExecutor are real. Runners live in a lightweight module so spawn
does not import this test module and the whole app. Every fixture joins its
workers; running-job assertions use an explicit gate, not a sleep window.
"""

from __future__ import annotations

import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import quantmind.api.routers.sync as sync_module
from quantmind.api.app import create_app
from quantmind.datastore.store import BarStore
from sync_workers import fast_fail, fast_ok, gated_sync


@pytest.fixture
def client(tmp_path):
    app = create_app(store=BarStore(tmp_path), benchmark="SPY", api_token="testtoken")
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"}) as api:
        try:
            yield api
        finally:
            manager = getattr(app.state, "job_manager", None)
            if manager is not None:
                # The public shutdown is nonblocking for app callers. Tests
                # must join their actual workers before the next case starts.
                workers = list((manager._pool._processes or {}).values())
                manager._pool.shutdown(wait=True, cancel_futures=True)
                assert not any(worker.is_alive() for worker in workers)


@pytest.fixture
def blocked_sync(tmp_path):
    started = tmp_path / "sync-started"
    release = tmp_path / "sync-release"
    try:
        yield partial(gated_sync, started, release), started, release
    finally:
        release.touch()  # also release on an assertion failure before teardown


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/sync/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["state"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.02)
    raise TimeoutError(f"sync job {job_id} did not finish within {timeout}s; last status: {body}")


def test_submit_returns_job_id_immediately(client, monkeypatch, blocked_sync):
    runner, _, _ = blocked_sync
    monkeypatch.setattr(sync_module, "_run_sync_cli", runner)
    r = client.post("/api/sync")
    assert r.status_code == 200
    assert isinstance(r.json()["job_id"], str) and r.json()["job_id"]
    assert client.get(f"/api/sync/{r.json()['job_id']}").json()["state"] == "running"


def test_status_lifecycle_reaches_done_with_result(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", fast_ok)
    job_id = client.post("/api/sync").json()["job_id"]
    body = _wait_terminal(client, job_id)
    assert body["state"] == "done"
    assert body["result"] == "synced 3 symbols"


def test_status_reports_error_message_on_failure(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", fast_fail)
    job_id = client.post("/api/sync").json()["job_id"]
    body = _wait_terminal(client, job_id)
    assert body["state"] == "error"
    assert "boom" in body["error"]


def test_double_submit_while_running_is_idempotent(client, monkeypatch, blocked_sync):
    runner, started, release = blocked_sync
    monkeypatch.setattr(sync_module, "_run_sync_cli", runner)
    a = client.post("/api/sync").json()["job_id"]
    deadline = time.monotonic() + 5
    while not started.exists():
        assert time.monotonic() < deadline, "sync worker never signalled that it started"
        time.sleep(0.005)
    b = client.post("/api/sync").json()["job_id"]
    assert a == b
    assert client.get(f"/api/sync/{a}").json()["state"] == "running"
    release.touch()
    assert _wait_terminal(client, a)["result"] == "synced 3 symbols"


def test_resubmit_after_completion_runs_a_fresh_sync(client, monkeypatch):
    # De-dup applies only while a sync is running. Once the first job is done,
    # "Sync now" must start a NEW job (new id, runner invoked again) — not
    # silently return the stale done job until TTL eviction (review finding).
    monkeypatch.setattr(sync_module, "_run_sync_cli", fast_ok)
    a = client.post("/api/sync").json()["job_id"]
    first = _wait_terminal(client, a)
    assert first["state"] == "done"

    b = client.post("/api/sync").json()["job_id"]
    assert b != a
    second = _wait_terminal(client, b)
    assert second["state"] == "done"
    assert second["result"] == "synced 3 symbols"  # the runner actually ran again


def test_unknown_job_id_is_404(client):
    r = client.get("/api/sync/does-not-exist")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_job_manager_created_exactly_once_under_concurrent_first_requests(monkeypatch):
    # I2 (TOCTOU): two simultaneous first calls into `_job_manager` must not
    # each observe `job_manager is None` and build their own JobManager (two
    # ProcessPoolExecutor pools racing sync_cli against one cache). Count
    # constructions directly, bypassing HTTP/TestClient entirely so the race
    # is on `_job_manager`'s own state, not on unrelated I/O.
    construction_count = 0
    construction_lock = threading.Lock()

    class _CountingJobManager:
        def __init__(self, *args, **kwargs):
            nonlocal construction_count
            # Widen the window between the None-check and the state-assignment
            # in `_job_manager` so a racing thread reliably observes
            # `job_manager is None` too, rather than relying on GIL luck.
            time.sleep(0.01)
            with construction_lock:
                construction_count += 1

    monkeypatch.setattr(sync_module, "JobManager", _CountingJobManager)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    n_threads = 32
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # maximize the odds every thread races the None-check together
        sync_module._job_manager(request)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert construction_count == 1
    assert isinstance(request.app.state.job_manager, _CountingJobManager)
