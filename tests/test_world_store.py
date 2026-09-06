"""World cache must survive source outages, restarts and concurrent refreshes."""
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
import sqlite3

import pytest

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _lease_contender(root: str, barrier, results) -> None:
    """Spawn-safe contender: synchronize once, acquire once, then exit."""
    from quantmind.world.store import WorldStore

    barrier.wait()
    results.put(WorldStore(Path(root)).acquire_lease(NOW, 180))


def event(key="one", source="fed", **overrides):
    from quantmind.world.models import WorldEvent
    return WorldEvent(**{
        "id": key, "source_id": source, "source_name": source,
        "title": "Interest rate decision", "url": f"https://example.org/{key}",
        "summary": "Policy update", "published_at": NOW.isoformat(),
        "topics": ["rates"], "regions": ["US"], **overrides,
    })


def test_success_survives_restart_and_failure_keeps_last_good_items(tmp_path):
    from quantmind.world.store import WorldStore
    cache = WorldStore(tmp_path)
    cache.record_success("fed", [event()], NOW, 900)
    reopened = WorldStore(tmp_path)
    reopened.record_failure("fed", "Request timed out", NOW + timedelta(minutes=20), 900)
    assert [e.id for e in reopened.items(NOW + timedelta(minutes=20))] == ["one"]
    state = reopened.states()["fed"]
    assert state["state"] == "error"
    assert state["last_success"] == NOW.isoformat()
    assert state["error"] == "Request timed out"
    assert state["item_count"] == 1


def test_observed_entries_keep_first_seen_time_on_repeated_refresh(tmp_path):
    from quantmind.world.store import WorldStore
    cache = WorldStore(tmp_path)
    cache.record_success("fed", [event(time_kind="observed")], NOW, 900)
    later = NOW + timedelta(hours=2)
    cache.record_success("fed", [event(time_kind="observed", published_at=later.isoformat())], later, 900)
    assert cache.items(later)[0].published_at == NOW.isoformat()
    assert cache.states()["fed"]["item_count"] == 1


def test_retention_and_fair_read_dont_let_noisy_source_evict_official_feed(tmp_path):
    from quantmind.world.store import WorldStore
    cache = WorldStore(tmp_path)
    cache.record_success("fed", [event()], NOW, 900)
    cache.record_success("gdelt", [event(f"g{i}", "gdelt") for i in range(400)], NOW, 900)
    rows = cache.items(NOW)
    assert "fed" in {e.source_id for e in rows}
    assert len([e for e in rows if e.source_id == "gdelt"]) == 30
    assert cache.states()["gdelt"]["item_count"] == 250
    assert cache.items(NOW + timedelta(days=31)) == []


def test_refresh_lease_crosses_instances_expires_and_owner_cannot_release_successor(tmp_path):
    from quantmind.world.store import WorldStore
    first, second = WorldStore(tmp_path), WorldStore(tmp_path)
    token = first.acquire_lease(NOW, 180)
    assert token
    assert second.acquire_lease(NOW, 180) is None
    assert second.refreshing(NOW)
    later = NOW + timedelta(seconds=181)
    successor = second.acquire_lease(later, 180)
    assert successor and successor != token
    first.release_lease(token)
    assert second.refreshing(later)
    second.release_lease(successor)
    assert not first.refreshing(later)


def test_refresh_lease_has_exactly_one_winner_across_spawned_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [
        context.Process(target=_lease_contender, args=(str(tmp_path), barrier, results))
        for _ in range(2)
    ]

    for child in children:
        child.start()
    for child in children:
        child.join(timeout=10)
    try:
        assert all(not child.is_alive() for child in children), "lease contenders did not finish"
        assert [child.exitcode for child in children] == [0, 0]
        tokens = [results.get(timeout=1) for _ in children]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=2)
        results.close()
        results.join_thread()

    assert sum(token is not None for token in tokens) == 1


def test_exited_process_lease_blocks_until_ttl_without_heartbeat_or_sleep(tmp_path):
    from quantmind.world.store import WorldStore

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(1)
    results = context.Queue()
    owner = context.Process(target=_lease_contender, args=(str(tmp_path), barrier, results))
    owner.start()
    owner.join(timeout=10)
    try:
        assert not owner.is_alive(), "lease owner did not finish"
        assert owner.exitcode == 0
        assert results.get(timeout=1) is not None
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(timeout=2)
        results.close()
        results.join_thread()

    contender = WorldStore(tmp_path)
    assert contender.acquire_lease(NOW + timedelta(seconds=179), 180) is None
    assert contender.acquire_lease(NOW + timedelta(seconds=181), 180) is not None


def test_profile_persists_local_normalized_preferences(tmp_path):
    from quantmind.world.models import WorldProfile
    from quantmind.world.store import WorldStore
    cache = WorldStore(tmp_path)
    cache.save_profile(WorldProfile(watch_symbols=["nvda"], interests=["semiconductors"], regions=["Europe"]))
    assert WorldStore(tmp_path).profile().watch_symbols == ["NVDA"]


def test_unknown_schema_fails_closed_without_overwriting_user_data(tmp_path):
    from quantmind.world.store import WorldStore, WorldStoreError
    cache = WorldStore(tmp_path)
    cache.profile()
    with sqlite3.connect(tmp_path / "world.sqlite3") as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(WorldStoreError, match="newer"):
        WorldStore(tmp_path).profile()


def test_source_write_rolls_back_on_invalid_batch(tmp_path):
    from quantmind.world.store import WorldStore
    cache = WorldStore(tmp_path)
    cache.record_success("fed", [event()], NOW, 900)
    with pytest.raises(ValueError, match="source"):
        cache.record_success("fed", [event("two"), event("wrong", "gdelt")], NOW, 900)
    assert [e.id for e in cache.items(NOW)] == ["one"]


def test_corrupt_event_timestamp_is_skipped_before_ranking(tmp_path):
    import json

    from quantmind.world.service import WorldService
    from quantmind.world.store import WorldStore

    cache = WorldStore(tmp_path)
    cache.profile()
    payload = event().model_dump()
    payload["published_at"] = "not-a-date"
    with sqlite3.connect(tmp_path / "world.sqlite3") as db:
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            ("bad", "fed", NOW.timestamp(), json.dumps(payload)),
        )

    assert WorldService(cache).snapshot([], None)["items"] == []


def test_corrupt_source_status_is_exposed_as_safe_error_state(tmp_path):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore

    cache = WorldStore(tmp_path)
    cache.profile()
    with sqlite3.connect(tmp_path / "world.sqlite3") as db:
        db.execute(
            "INSERT INTO source_state VALUES (?, ?, ?, ?, ?, ?)",
            ("fed", "garbage", "bad-attempt", "bad-success", "bad-next", "unsafe details"),
        )

    state = WorldService(cache, sources=SOURCES[:1], clock=lambda: NOW).snapshot([], None)["sources"][0]
    assert state["state"] == "error"
    assert state["last_attempt"] is None
    assert state["last_success"] is None
    assert state["next_refresh"] is None
    assert state["error"] == "Cached source status was invalid. Refresh this source."
