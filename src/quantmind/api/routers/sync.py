"""sync domain routes — one-shot cache sync exposed as a background job.

POST /api/sync submits `python -m quantmind.sync_cli` as a subprocess via a
JobManager lazily created on `request.app.state` (Task 4 pattern: reuse the
existing job primitive, don't rebuild it — see api/jobs.py). max_workers=1
keeps at most one sync running; idempotency_key="sync" means a double-click
(or a second tab) joins the already-tracked job instead of spawning a second
sync process, per the wave-2 plan.

GET /api/sync/{job_id} is a thin passthrough of JobManager.status; an unknown
id (never submitted, or evicted after TTL) is a 404, not a 500.
"""

from __future__ import annotations

import subprocess
import sys

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from quantmind.api.jobs import JobManager

router = APIRouter()


def _run_sync_cli() -> str:
    """Runs inside the JobManager's process-pool worker — must stay a
    picklable top-level function (no closures, no request/app state)."""
    result = subprocess.run(
        [sys.executable, "-m", "quantmind.sync_cli"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-4000:]
        raise RuntimeError(f"sync_cli exited {result.returncode}: {tail}")
    return (result.stdout or "").strip()[-4000:]


class SyncSubmitResponse(BaseModel):
    job_id: str


class SyncStatusResponse(BaseModel):
    state: str
    result: str | None = None
    error: str | None = None


def _job_manager(request: Request) -> JobManager:
    jm = getattr(request.app.state, "job_manager", None)
    if jm is None:
        jm = JobManager(max_workers=1)
        request.app.state.job_manager = jm
    return jm


@router.post("/sync", response_model=SyncSubmitResponse)
def submit_sync(request: Request) -> SyncSubmitResponse:
    jm = _job_manager(request)
    job_id = jm.submit(_run_sync_cli, idempotency_key="sync")
    return SyncSubmitResponse(job_id=job_id)


@router.get("/sync/{job_id}", response_model=SyncStatusResponse)
def sync_status(job_id: str, request: Request) -> SyncStatusResponse:
    jm = _job_manager(request)
    try:
        status = jm.status(job_id)
    except KeyError:
        raise HTTPException(404, detail=f"unknown sync job id {job_id!r}")
    return SyncStatusResponse(
        state=status["state"],
        result=status.get("result"),
        error=status.get("error"),
    )
