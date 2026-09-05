"""Network-boundary orchestration: isolation, privacy, cadence and single-flight."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import httpx
import pytest

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
FEED = b'<rss><channel><item><title>Nvidia market update</title><link>https://example.org/news</link><pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>'


async def test_refresh_isolates_failure_and_respects_cadence(tmp_path):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore
    fed, ecb = SOURCES[:2]
    def handle(request):
        if request.url.host == "www.federalreserve.gov":
            return httpx.Response(200, content=FEED)
        return httpx.Response(429, headers={"Retry-After": "3600"})
    service = WorldService(WorldStore(tmp_path), sources=(fed, ecb),
                           transport=httpx.MockTransport(handle), clock=lambda: NOW)
    assert await service.refresh() == {"updated": 1, "failed": 1, "skipped": 0}
    assert await service.refresh() == {"updated": 0, "failed": 0, "skipped": 2}
    snapshot = service.snapshot([], None)
    assert snapshot["items"][0].title == "Nvidia market update"
    assert snapshot["sources"][1]["state"] == "error"
    assert snapshot["sources"][1]["next_refresh"] == (NOW + timedelta(hours=1)).isoformat()
    assert snapshot["as_of"] == NOW.isoformat()


async def test_double_refresh_joins_one_run_and_cached_snapshot_remains_available(tmp_path):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, content=FEED)
    service = WorldService(WorldStore(tmp_path), sources=SOURCES[:1],
                           transport=httpx.MockTransport(handle), clock=lambda: NOW)
    first = asyncio.create_task(service.refresh())
    await entered.wait()
    second = asyncio.create_task(service.refresh())
    assert service.snapshot([], None)["refreshing"]
    release.set()
    results = await asyncio.gather(first, second)
    assert results == [{"updated": 1, "failed": 0, "skipped": 0}] * 2
    assert calls == 1  # duplicate network requests would double-charge gated sources
    assert not service.snapshot([], None)["refreshing"]


async def test_source_concurrency_is_bounded_to_four(tmp_path):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore
    active = peak = 0
    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=FEED)
    service = WorldService(WorldStore(tmp_path),
        sources=tuple(replace(SOURCES[0], id=f"source{i}") for i in range(9)),
        transport=httpx.MockTransport(handle), clock=lambda: NOW)
    assert (await service.refresh())["updated"] == 9
    assert peak == 4


async def test_disabled_social_sources_never_make_requests_and_no_holdings_leave_machine(tmp_path):
    from quantmind.world.models import WorldProfile
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore
    outbound = []
    def handle(request):
        outbound.append(str(request.url))
        return httpx.Response(200, content=FEED)
    cache = WorldStore(tmp_path)
    cache.save_profile(WorldProfile(watch_symbols=["NVDA"]))
    service = WorldService(cache, sources=tuple(s for s in SOURCES if s.id in {"fed", "x", "reddit"}),
        transport=httpx.MockTransport(handle), clock=lambda: NOW)
    assert (await service.refresh())["skipped"] == 2
    assert len(outbound) == 1 and "NVDA" not in outbound[0]
    result = service.snapshot(["ASML"], "abcdef012345")
    assert result["context"]["symbols"] == ["ASML"]
    assert "Watchlist NVDA: company name mentioned" in result["items"][0].reasons


async def test_unexpected_provider_failure_is_redacted_and_releases_lease(tmp_path):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore
    def handle(request):
        raise RuntimeError("Bearer private-secret must not reach UI")
    service = WorldService(WorldStore(tmp_path), sources=SOURCES[:1],
        transport=httpx.MockTransport(handle), clock=lambda: NOW)
    assert (await service.refresh())["failed"] == 1
    result = service.snapshot([], None)
    assert "private-secret" not in str(result)
    assert not result["refreshing"]


async def test_state_load_failure_after_acquiring_lease_releases_it(tmp_path, monkeypatch):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore, WorldStoreError

    cache = WorldStore(tmp_path)
    service = WorldService(cache, sources=SOURCES[:1], clock=lambda: NOW)

    def fail_states():
        raise WorldStoreError("state catalog unavailable")

    monkeypatch.setattr(cache, "states", fail_states)
    with pytest.raises(WorldStoreError, match="state catalog unavailable"):
        await service.refresh()

    assert not cache.refreshing(NOW)
    assert service._refresh_task is None


async def test_lease_release_failure_does_not_mask_active_refresh_failure(tmp_path, monkeypatch):
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore, WorldStoreError

    cache = WorldStore(tmp_path)
    service = WorldService(cache, sources=SOURCES[:1], clock=lambda: NOW)
    monkeypatch.setattr(
        cache, "states",
        lambda: (_ for _ in ()).throw(WorldStoreError("primary state failure")),
    )
    monkeypatch.setattr(
        cache, "release_lease",
        lambda _owner: (_ for _ in ()).throw(WorldStoreError("secondary release failure")),
    )

    with pytest.raises(WorldStoreError, match="primary state failure"):
        await service.refresh()


@pytest.mark.asyncio
async def test_blocked_store_call_does_not_block_the_api_event_loop(tmp_path, monkeypatch):
    """A contended SQLite busy wait must not freeze unrelated async endpoints."""
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore

    cache = WorldStore(tmp_path)
    original_states = cache.states

    def blocked_states():
        time.sleep(0.3)
        return original_states()

    monkeypatch.setattr(cache, "states", blocked_states)
    service = WorldService(cache, sources=SOURCES[:1], clock=lambda: NOW)
    refresh = asyncio.create_task(service.refresh())
    started = asyncio.get_running_loop().time()
    await asyncio.sleep(0.02)  # stand-in for an unrelated async API handler
    elapsed = asyncio.get_running_loop().time() - started
    await refresh
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_cancellation_during_threaded_lease_acquire_releases_owner(tmp_path, monkeypatch):
    """Cancelling the coroutine cannot abandon a lease acquired by its worker."""
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES
    from quantmind.world.store import WorldStore

    cache = WorldStore(tmp_path)
    original_acquire = cache.acquire_lease

    def delayed_acquire(now):
        time.sleep(0.1)
        return original_acquire(now)

    monkeypatch.setattr(cache, "acquire_lease", delayed_acquire)
    service = WorldService(cache, sources=SOURCES[:1], clock=lambda: NOW)
    refresh = asyncio.create_task(service._run_refresh())
    await asyncio.sleep(0.01)
    refresh.cancel()

    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert not cache.refreshing(NOW)


@pytest.mark.asyncio
async def test_persistence_failure_drains_sibling_before_releasing_lease(monkeypatch):
    """One failed cache write cannot orphan another source outside the lease."""
    import quantmind.world.service as service_module
    from quantmind.world.providers import ProviderError
    from quantmind.world.service import WorldService
    from quantmind.world.sources import SOURCES

    slow_started = asyncio.Event()
    unblock_slow = asyncio.Event()

    class FailingCache:
        def __init__(self):
            self.released = False
            self.success_after_release = None

        def acquire_lease(self, _now):
            return "owner"

        def states(self):
            return {}

        def record_failure(self, *_args):
            raise RuntimeError("source state write failed")

        def record_success(self, *_args):
            self.success_after_release = self.released

        def release_lease(self, _owner):
            self.released = True

    async def fake_fetch(source, _client, _config, _now):
        if source.id == "bad":
            raise ProviderError("provider failed")
        slow_started.set()
        await unblock_slow.wait()
        return []

    monkeypatch.setattr(service_module, "fetch_source", fake_fetch)
    cache = FailingCache()
    service = WorldService(cache, sources=(
        replace(SOURCES[0], id="bad"), replace(SOURCES[1], id="slow"),
    ), clock=lambda: NOW)

    refresh = asyncio.create_task(service._run_refresh())
    await slow_started.wait()
    await asyncio.sleep(0.02)
    assert not refresh.done()
    assert not cache.released

    unblock_slow.set()
    with pytest.raises(RuntimeError, match="source state write failed"):
        await refresh
    assert cache.success_after_release is False
    assert cache.released
