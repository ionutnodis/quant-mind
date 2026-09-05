"""Independent, bounded world cache. No portfolio/bar writes or network access.

Each operation owns its SQLite connection. Transactions and a leased refresh
owner cover threads, multiple API processes and interrupted refreshes alike.
Schema version 1 is created only for an empty/new database; unknown future
versions fail closed rather than rewriting a user's cache.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from quantmind.world.models import WorldEvent, WorldProfile


class WorldStoreError(RuntimeError):
    pass


LEASE_CLOCK_SKEW_SECONDS = 60


def _active_lease_expiry(row: sqlite3.Row | None, now: datetime, ttl: int) -> float | None:
    if row is None:
        return None
    owner = row["owner"]
    try:
        expires = float(row["expires"])
    except (TypeError, ValueError):
        raise WorldStoreError("World cache refresh lease is invalid; restart with a clean cache.") from None
    if (
        not isinstance(owner, str)
        or re.fullmatch(r"[0-9a-f]{32}", owner) is None
        or not math.isfinite(expires)
        or expires > now.timestamp() + ttl + LEASE_CLOCK_SKEW_SECONDS
    ):
        raise WorldStoreError("World cache refresh lease is invalid; restart with a clean cache.")
    return expires


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp.utcoffset() == timedelta(0)


class WorldStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "world.sqlite3"

    @contextmanager
    def _db(self):
        db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=3)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 3000")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise WorldStoreError("World cache uses a newer schema; upgrade QuantMind.")
            # SQLite's busy_timeout is not consistently honored while two fresh
            # processes race to switch the same database into WAL mode. Retry
            # only that transient lock within the existing three-second bound.
            wal_deadline = time.monotonic() + 3
            while True:
                try:
                    db.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= wal_deadline:
                        raise
                    time.sleep(0.025)
            if version == 0:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                        event_time REAL NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS event_source_time
                        ON events(source_id, event_time DESC);
                    CREATE TABLE IF NOT EXISTS source_state (
                        source_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                        last_attempt TEXT, last_success TEXT,
                        next_refresh TEXT, error TEXT
                    );
                    CREATE TABLE IF NOT EXISTS preferences (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS refresh_lease (
                        id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, expires REAL
                    );
                    PRAGMA user_version = 1;
                    COMMIT;
                """)
            with db:
                yield db
        except (sqlite3.Error, OSError) as exc:
            raise WorldStoreError(
                "World cache unavailable. Check data-directory access and disk space; "
                "other portfolio tools are unaffected."
            ) from exc
        finally:
            if db is not None:
                db.close()

    def profile(self) -> WorldProfile:
        with self._db() as db:
            row = db.execute("SELECT payload FROM preferences WHERE id=1").fetchone()
        try:
            return WorldProfile.model_validate_json(row[0]) if row else WorldProfile()
        except ValueError as exc:
            raise WorldStoreError("Saved World preferences are invalid; save a new lens.") from exc

    def save_profile(self, profile: WorldProfile) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES (1, ?)", (profile.model_dump_json(),))

    def states(self) -> dict[str, dict]:
        with self._db() as db:
            rows = db.execute("""
                SELECT s.*, (SELECT COUNT(*) FROM events e
                    WHERE e.source_id=s.source_id) AS item_count FROM source_state s
            """).fetchall()
        states = {}
        for row in rows:
            state = dict(row)
            timestamps_valid = all(
                state[field] is None or _valid_utc_timestamp(state[field])
                for field in ("last_attempt", "last_success", "next_refresh")
            )
            if state["state"] not in {"ok", "error"} or not timestamps_valid:
                state.update(
                    state="error",
                    last_attempt=None,
                    last_success=None,
                    next_refresh=None,
                    error="Cached source status was invalid. Refresh this source.",
                )
            states[state["source_id"]] = state
        return states

    def items(self, now: datetime, *, per_source: int = 30) -> list[WorldEvent]:
        cutoff = (now - timedelta(days=30)).timestamp()
        with self._db() as db:
            rows = db.execute("""
                SELECT payload, event_time FROM (
                    SELECT payload, event_time, id, ROW_NUMBER() OVER (
                        PARTITION BY source_id ORDER BY event_time DESC, id
                    ) AS rank FROM events WHERE event_time BETWEEN ? AND ?
                ) WHERE rank <= ? ORDER BY event_time DESC, id LIMIT 500
            """, (cutoff, now.timestamp(), min(max(per_source, 1), 100))).fetchall()
        items = []
        for row in rows:
            try:
                event = WorldEvent.model_validate_json(row["payload"])
                stamp = datetime.fromisoformat(event.published_at.replace("Z", "+00:00"))
                stored_stamp = float(row["event_time"])
                if not cutoff <= stamp.timestamp() <= now.timestamp():
                    continue
                if abs(stamp.timestamp() - stored_stamp) > 1:
                    continue
                items.append(event)
            except (TypeError, ValueError):
                # A damaged record must not take out otherwise usable feeds.
                continue
        return items

    def record_success(self, source_id: str, events: list[WorldEvent], now: datetime, interval: int) -> None:
        if any(event.source_id != source_id for event in events):
            raise ValueError("event source does not match the source transaction")
        cutoff = (now - timedelta(days=30)).timestamp()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for event in events:
                stamp = datetime.fromisoformat(event.published_at.replace("Z", "+00:00"))
                if stamp.tzinfo is None or not cutoff <= stamp.timestamp() <= now.timestamp():
                    continue
                previous = db.execute(
                    "SELECT source_id, event_time, payload FROM events WHERE id=?", (event.id,)
                ).fetchone()
                if previous and event.time_kind == "observed":
                    # Polling an undated feed must not continuously bump old
                    # entries to the top, nor downgrade known publication time.
                    # Damaged cache bytes are never authoritative: a valid
                    # fresh fetch must be able to repair them transactionally.
                    try:
                        old = WorldEvent.model_validate_json(previous["payload"])
                        old_stamp = datetime.fromisoformat(
                            old.published_at.replace("Z", "+00:00")
                        )
                        stored_stamp = float(previous["event_time"])
                        valid_previous = (
                            previous["source_id"] == source_id
                            and old.id == event.id
                            and old.source_id == source_id
                            and cutoff <= old_stamp.timestamp() <= now.timestamp()
                            and abs(old_stamp.timestamp() - stored_stamp) <= 1
                        )
                    except (TypeError, ValueError):
                        valid_previous = False
                    if valid_previous:
                        event = event.model_copy(update={
                            "published_at": old.published_at,
                            "time_kind": old.time_kind,
                        })
                        stamp = old_stamp
                db.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?)", (
                    event.id, source_id, stamp.timestamp(), event.model_dump_json(),
                ))
            db.execute("DELETE FROM events WHERE event_time < ?", (cutoff,))
            db.execute("""DELETE FROM events WHERE id IN (
                SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY source_id ORDER BY event_time DESC, id
                ) AS rank FROM events) WHERE rank > 250
            )""")
            db.execute("""DELETE FROM events WHERE id IN (
                SELECT id FROM events ORDER BY event_time DESC, id LIMIT -1 OFFSET 5000
            )""")
            db.execute("""INSERT INTO source_state VALUES (?, 'ok', ?, ?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET state='ok',
                last_attempt=excluded.last_attempt, last_success=excluded.last_success,
                next_refresh=excluded.next_refresh, error=NULL""", (
                    source_id, now.isoformat(), now.isoformat(),
                    (now + timedelta(seconds=interval)).isoformat(),
                ))

    def record_failure(self, source_id: str, error: str, now: datetime, cooldown: int) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO source_state VALUES (?, 'error', ?, NULL, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET state='error',
                last_attempt=excluded.last_attempt, next_refresh=excluded.next_refresh,
                error=excluded.error""", (source_id, now.isoformat(),
                    (now + timedelta(seconds=cooldown)).isoformat(), error[:300]))

    def acquire_lease(self, now: datetime, ttl: int = 180) -> str | None:
        owner = uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner, expires FROM refresh_lease WHERE id=1"
            ).fetchone()
            expires = _active_lease_expiry(row, now, ttl)
            if expires is not None and expires > now.timestamp():
                return None
            db.execute("INSERT OR REPLACE INTO refresh_lease VALUES (1, ?, ?)",
                       (owner, now.timestamp() + ttl))
        return owner

    def release_lease(self, owner: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM refresh_lease WHERE owner=?", (owner,))

    def refreshing(self, now: datetime) -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT owner, expires FROM refresh_lease WHERE id=1"
            ).fetchone()
            expires = _active_lease_expiry(row, now, 180)
            return expires is not None and expires > now.timestamp()
