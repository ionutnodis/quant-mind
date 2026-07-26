"""JobManager lifecycle: the minimal job pattern, hardened per Codex #6."""

import time

import pytest

from quantmind.api.jobs import JobManager


def _quick(x):
    return x * 2


def _slow(x):
    time.sleep(0.4)
    return x + 1


def _boom(_):
    raise ValueError("deliberate failure")


@pytest.fixture
def jm():
    m = JobManager(max_workers=2, ttl_seconds=60)
    yield m
    m.shutdown()


def _wait_done(jm, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = jm.status(job_id)
        if s["state"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.02)
    raise TimeoutError


def test_submit_poll_done_result(jm):
    job_id = jm.submit(_quick, 21)
    s = _wait_done(jm, job_id)
    assert s["state"] == "done"
    assert s["result"] == 42


def test_error_jobs_report_error_state_and_message(jm):
    job_id = jm.submit(_boom, 0)
    s = _wait_done(jm, job_id)
    assert s["state"] == "error"
    assert "deliberate failure" in s["error"]


def test_concurrent_jobs_both_complete(jm):
    ids = [jm.submit(_slow, i) for i in range(4)]  # 4 jobs, 2 workers
    results = sorted(_wait_done(jm, i)["result"] for i in ids)
    assert results == [1, 2, 3, 4]


def test_idempotent_submission_by_key(jm):
    a = jm.submit(_slow, 1, idempotency_key="same")
    b = jm.submit(_slow, 1, idempotency_key="same")
    assert a == b  # second submit returns the existing job


def test_key_resubmit_after_terminal_job_starts_a_new_job(jm):
    # Idempotency de-dups only while the mapped job is RUNNING. Once it is
    # terminal, the same key must submit a FRESH job and remap — otherwise a
    # finished job pins its key until TTL eviction and every resubmit is a
    # silent no-op (wave-2 Task 4 review finding).
    a = jm.submit(_quick, 1, idempotency_key="same")
    _wait_done(jm, a)
    b = jm.submit(_quick, 2, idempotency_key="same")
    assert b != a  # terminal job does not absorb the resubmission
    s = _wait_done(jm, b)
    assert s["result"] == 4  # the new fn actually ran
    # and the key now maps to the new job: once b is terminal too, a third
    # submit spawns yet another fresh job rather than returning a or b
    c = jm.submit(_quick, 3, idempotency_key="same")
    assert c not in (a, b)
    _wait_done(jm, c)


def test_unknown_job_id_raises(jm):
    with pytest.raises(KeyError):
        jm.status("nonexistent")


def test_ttl_eviction():
    m = JobManager(max_workers=1, ttl_seconds=0)
    job_id = m.submit(_quick, 1)
    _wait_done(m, job_id)
    m.evict_expired(now=time.time() + 1)
    with pytest.raises(KeyError):
        m.status(job_id)
    m.shutdown()
