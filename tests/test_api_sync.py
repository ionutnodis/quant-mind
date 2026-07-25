"""POST /api/sync submits a job that runs sync_cli in a background worker;
GET /api/sync/{job_id} passes through JobManager status. Contract: submit
returns an id immediately; status lifecycle transitions running -> done/error;
double-submit while the first job is still tracked is idempotent (same job
id — the "double-click joins the running job" requirement); unknown id -> 404.

The runner (`_run_sync_cli`) is monkeypatched to a fast, picklable top-level
function (ProcessPoolExecutor pickles by module+qualname reference, same
pattern as tests/test_jobs.py) so these tests never touch a real subprocess
or IB Gateway.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import quantmind.api.routers.sync as sync_module
from quantmind.api.app import create_app
from quantmind.datastore.store import BarStore


def _fast_ok() -> str:
    return "synced 3 symbols"


def _fast_fail() -> str:
    raise RuntimeError("sync_cli exited 1: boom")


@pytest.fixture
def client(tmp_path):
    app = create_app(store=BarStore(tmp_path), benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/sync/{job_id}").json()
        if body["state"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.02)
    raise TimeoutError


def test_submit_returns_job_id_immediately(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", _fast_ok)
    r = client.post("/api/sync")
    assert r.status_code == 200
    assert isinstance(r.json()["job_id"], str) and r.json()["job_id"]


def test_status_lifecycle_reaches_done_with_result(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", _fast_ok)
    job_id = client.post("/api/sync").json()["job_id"]
    body = _wait_terminal(client, job_id)
    assert body["state"] == "done"
    assert body["result"] == "synced 3 symbols"


def test_status_reports_error_message_on_failure(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", _fast_fail)
    job_id = client.post("/api/sync").json()["job_id"]
    body = _wait_terminal(client, job_id)
    assert body["state"] == "error"
    assert "boom" in body["error"]


def test_double_submit_is_idempotent(client, monkeypatch):
    monkeypatch.setattr(sync_module, "_run_sync_cli", _fast_ok)
    a = client.post("/api/sync").json()["job_id"]
    b = client.post("/api/sync").json()["job_id"]
    assert a == b
    _wait_terminal(client, a)  # drain so the pool shuts down cleanly


def test_unknown_job_id_is_404(client):
    r = client.get("/api/sync/does-not-exist")
    assert r.status_code == 404
    assert "detail" in r.json()
