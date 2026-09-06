"""Bounded synthetic CPU benchmark; no cache, broker, or network access.

Run: uv run python scripts/world_ranking_benchmark.py --runs 3
Timing is diagnostic, never a machine-dependent CI pass/fail threshold.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from statistics import median
from time import perf_counter

from quantmind.world.models import WorldConfig, WorldEvent, WorldProfile
from quantmind.world.relevance import rank_events
from quantmind.world.sources import SOURCES, source_enabled


def fixture() -> tuple[list[WorldEvent], list[str], WorldProfile, datetime]:
    sources = [source for source in SOURCES if source_enabled(source, WorldConfig())]
    events = [WorldEvent(
        id=f"{source.id}-{index}", source_id=source.id, source_name=source.name,
        title=("Global market conditions and policy commentary. " * 8)[:300],
        summary=("This synthetic event describes economic developments without mentioning a watch symbol. " * 8)[:500],
        published_at="2026-09-06T09:00:00Z", topics=list(source.topics),
        regions=list(source.regions), url=f"https://example.org/{source.id}/{index}",
    ) for source in sources for index in range(30)]
    symbols = ["NVDA", "ASML", "MSFT", "SPY", "QQQ", "AMD", "TSM", "MU", "AAPL", "GOOGL"]
    profile = WorldProfile(
        watch_symbols=[f"WATCH{index:03d}" for index in range(100)],
        interests=[f"topic{index}" for index in range(20)],
        regions=[f"REGION{index}" for index in range(20)],
    )
    return events, symbols, profile, datetime(2026, 9, 6, 10, tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    events, symbols, profile, now = fixture()
    durations = []
    for _ in range(args.runs):
        started = perf_counter()
        ranked = rank_events(events, symbols, profile, now)
        durations.append(perf_counter() - started)
        assert len(ranked) == len(events)
        assert all(not event.reasons and event.relevance == 0 for event in ranked)
    print(json.dumps({
        "events": len(events), "watch_symbols": len(profile.watch_symbols),
        "interests": len(profile.interests), "regions": len(profile.regions),
        "holdings": len(symbols), "seconds": durations,
        "median_seconds": median(durations),
    }))


if __name__ == "__main__":
    main()
