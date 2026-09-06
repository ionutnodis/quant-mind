"""Bounded refresh orchestration over independent providers and cached reads."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sys
from typing import Callable

import httpx

from quantmind.world.models import WorldConfig
from quantmind.world.providers import ProviderError, fetch_source
from quantmind.world.relevance import rank_events
from quantmind.world.sources import SOURCES, Source, source_enabled, source_setup_note
from quantmind.world.store import WorldStore


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except ValueError:
        return None


async def _store_call(call, *args):
    """Run one SQLite operation off-loop and drain it before cancellation.

    `asyncio.to_thread` cannot stop its worker. Draining the shielded task
    prevents lease release from racing an abandoned write.
    """
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _acquire_lease(cache: WorldStore, now: datetime) -> str | None:
    """Acquire off-loop without leaking a lease if the waiter is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(cache.acquire_lease, now))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, str):
            await _store_call(cache.release_lease, result)
        raise


class WorldService:
    def __init__(self, cache: WorldStore, config: WorldConfig | None = None, *,
                 sources: tuple[Source, ...] = SOURCES,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.cache, self.config, self.sources = cache, config or WorldConfig(), sources
        self.transport, self.clock = transport, clock
        self._refresh_task: asyncio.Task | None = None

    def snapshot(self, symbols: list[str], book_ref: str | None) -> dict:
        now = self.clock()
        states = self.cache.states()
        status = []
        successes = []
        for source in self.sources:
            enabled = source_enabled(source, self.config)
            state = states.get(source.id, {})
            success = _timestamp(state.get("last_success"))
            if success and success <= now:
                successes.append(success)
            status.append({
                "id": source.id, "name": source.name, "category": source.category,
                "homepage": source.homepage, "access": source.access,
                "description": source.description, "enabled": enabled,
                "state": state.get("state", "never") if enabled else "disabled",
                "last_attempt": state.get("last_attempt"),
                "last_success": state.get("last_success"),
                "next_refresh": state.get("next_refresh"),
                "item_count": state.get("item_count", 0),
                "error": state.get("error") if enabled else source_setup_note(source, self.config),
                "stale": enabled and (success is None or success > now or
                    (now - success).total_seconds() > max(1800, source.interval_seconds * 2)),
            })
        profile = self.cache.profile()
        enabled_ids = {source.id for source in self.sources if source_enabled(source, self.config)}
        events = [event for event in self.cache.items(now) if event.source_id in enabled_ids]
        return {
            "items": rank_events(events, symbols, profile, now), "sources": status,
            "profile": profile,
            "context": {"book_ref": book_ref, "symbols": symbols,
                        "label": f"Pinned book {book_ref}" if book_ref else "Saved personal lens; no book selected"},
            "as_of": max(successes).isoformat() if successes else None,
            "refreshing": self.cache.refreshing(now),
        }

    async def refresh(self) -> dict[str, int]:
        # No await between test/create: one task for same-process callers.
        # Shield avoids a browser disconnect cancelling everyone else's refresh.
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._run_refresh())
        task = self._refresh_task
        try:
            result = await asyncio.shield(task)
        except Exception:
            if task.done() and self._refresh_task is task:
                self._refresh_task = None
            raise
        if self._refresh_task is task:
            self._refresh_task = None
        return result

    async def _run_refresh(self) -> dict[str, int]:
        now = self.clock()
        owner = await _acquire_lease(self.cache, now)
        counts = {"updated": 0, "failed": 0, "skipped": 0}
        if owner is None:
            return {**counts, "skipped": len(self.sources)}
        try:
            states = await _store_call(self.cache.states)
            semaphore = asyncio.Semaphore(4)
            # Client lifetime is one bounded refresh; GET/profile never creates
            # one. No shared sockets across event loops or process workers.
            async with httpx.AsyncClient(
                transport=self.transport, timeout=8, follow_redirects=False,
                headers={"User-Agent": "QuantMind-World/1.0 (+https://github.com/ionutnodis/quant-mind)",
                         "Accept-Encoding": "identity"},
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            ) as client:
                async def update(source: Source):
                    next_refresh = _timestamp(states.get(source.id, {}).get("next_refresh"))
                    if not source_enabled(source, self.config) or (next_refresh and next_refresh > now):
                        counts["skipped"] += 1
                        return
                    async with semaphore:
                        try:
                            # Defense in depth: provider contract includes auth
                            # and parsing; its own 12s timeout is the inner bound.
                            async with asyncio.timeout(15):
                                events = await fetch_source(source, client, self.config, self.clock())
                            await _store_call(
                                self.cache.record_success, source.id, events,
                                self.clock(), source.interval_seconds,
                            )
                            counts["updated"] += 1
                        except Exception as exc:
                            message = str(exc) if isinstance(exc, ProviderError) else "Source refresh failed; cached events retained."
                            retry_after = getattr(exc, "retry_after", None)
                            cooldown = max(source.interval_seconds,
                                           min(retry_after or 0, 7 * 24 * 3600))
                            await _store_call(
                                self.cache.record_failure, source.id, message,
                                self.clock(), cooldown,
                            )
                            counts["failed"] += 1
                async with asyncio.timeout(120):
                    results = await asyncio.gather(
                        *(update(source) for source in self.sources),
                        return_exceptions=True,
                    )
                    # Persistence failures are service failures, but every
                    # sibling must finish while the lease and client are live.
                    failure = next(
                        (result for result in results if isinstance(result, BaseException)),
                        None,
                    )
                    if failure is not None:
                        raise failure
        finally:
            exception_in_flight = sys.exc_info()[0] is not None
            try:
                await _store_call(self.cache.release_lease, owner)
            except Exception:
                if not exception_in_flight:
                    raise
        return counts

    async def shutdown(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
