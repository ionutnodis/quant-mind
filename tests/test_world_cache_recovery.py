"""Damaged observed rows are replaceable cache entries, not permanent poison."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from quantmind.world.models import WorldEvent
from quantmind.world.store import WorldStore

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=2)


def observed(when: datetime = NOW) -> WorldEvent:
    return WorldEvent(id="same", source_id="fed", source_name="Federal Reserve",
        title="Policy update", url="https://example.org/policy", summary="",
        published_at=when.isoformat(), time_kind="observed", topics=[], regions=[])


def seed_raw(cache: WorldStore, payload: object, *, event_time: float | str | None = None) -> None:
    cache.profile()
    with sqlite3.connect(cache.path) as db:
        db.execute("INSERT INTO events (id, source_id, event_time, payload) VALUES (?, ?, ?, ?)",
            ("same", "fed", NOW.timestamp() if event_time is None else event_time, json.dumps(payload)))


@pytest.mark.parametrize(("payload", "event_time"), [
    ({}, None),
    ({**observed().model_dump(), "published_at": "not-a-date"}, None),
    ({**observed().model_dump(), "url": "javascript:alert(1)"}, None),
    (observed(OLD).model_dump(), "not-a-number"),
    ({**observed().model_dump(), "id": "other"}, None),
    ({**observed().model_dump(), "source_id": "ecb"}, None),
], ids=["empty", "bad-timestamp", "unsafe-url", "bad-event-time", "wrong-id", "wrong-source"])
def test_corrupt_prior_observed_event_is_replaced_by_fresh_valid_event(tmp_path, payload, event_time):
    cache = WorldStore(tmp_path)
    seed_raw(cache, payload, event_time=event_time)

    cache.record_success("fed", [observed()], NOW, 900)

    recovered = cache.items(NOW)
    assert len(recovered) == 1
    assert recovered[0] == observed()
    assert cache.states()["fed"]["state"] == "ok"


def test_valid_prior_observed_event_keeps_its_first_observed_time(tmp_path):
    cache = WorldStore(tmp_path)
    cache.record_success("fed", [observed(OLD)], OLD, 900)
    cache.record_success("fed", [observed()], NOW, 900)
    assert cache.items(NOW)[0].published_at == OLD.isoformat()


@pytest.mark.parametrize(("owner", "expires"), [
    ("owner", None),
    ("owner", "not-a-number"),
    ("owner", float("inf")),
    (None, NOW.timestamp() + 60),
    ("owner", NOW.timestamp() + 10_000),
])
def test_corrupt_refresh_lease_fails_safely_instead_of_crashing_or_locking_forever(
    tmp_path, owner, expires,
):
    from quantmind.world.store import WorldStoreError

    cache = WorldStore(tmp_path)
    cache.profile()
    with sqlite3.connect(cache.path) as db:
        db.execute("INSERT INTO refresh_lease VALUES (1, ?, ?)", (owner, expires))

    with pytest.raises(WorldStoreError, match="refresh lease is invalid"):
        cache.acquire_lease(NOW, 180)
    with pytest.raises(WorldStoreError, match="refresh lease is invalid"):
        cache.refreshing(NOW)


def test_lease_created_just_after_snapshot_time_is_valid_within_bounded_skew(tmp_path):
    cache = WorldStore(tmp_path)
    acquired_at = NOW + timedelta(milliseconds=1)

    assert cache.acquire_lease(acquired_at, 180) is not None

    assert cache.refreshing(NOW)
    assert cache.acquire_lease(NOW, 180) is None
