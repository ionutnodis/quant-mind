"""Headless, bounded World Monitor refresh command."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
import sys
from typing import Callable, Sequence

import httpx

from quantmind.config import Settings
from quantmind.world.service import WorldService
from quantmind.world.sources import SOURCES, Source
from quantmind.world.store import WorldStore


def _interval(value: str) -> int:
    try:
        interval = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be an integer") from None
    if interval < 300:
        raise argparse.ArgumentTypeError("interval must be at least 300 seconds")
    return interval


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the local World Monitor cache")
    parser.add_argument("--watch", action="store_true", help="continue refreshing at a bounded interval")
    parser.add_argument("--interval", type=_interval, default=300, metavar="SECONDS")
    parser.add_argument("--source", action="append", choices=[source.id for source in SOURCES], dest="source_ids")
    return parser.parse_args(argv)


def _selected(source_ids: list[str] | None) -> tuple[Source, ...]:
    if not source_ids:
        return SOURCES
    catalog = {source.id: source for source in SOURCES}
    return tuple(catalog[source_id] for source_id in dict.fromkeys(source_ids))


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    service_factory: Callable[..., WorldService] = WorldService,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    args = parse_args(argv)
    app_settings = settings or Settings()
    sources = _selected(args.source_ids)
    config = app_settings.world_config()
    if config.x_enabled and any(source.id == "x" for source in sources):
        print("Warning: X recent search uses a paid API when configured and enabled.", file=sys.stderr)
    service = service_factory(
        WorldStore(app_settings.data_dir), config, sources=sources,
        transport=transport, clock=clock,
    )
    exit_code = 0
    try:
        while True:
            result = await service.refresh()
            snapshot = service.snapshot([], None)
            state_counts = Counter(str(item.get("state", "never")) for item in snapshot["sources"])
            print(json.dumps({
                "at": _utc_text(clock()),
                "result": result,
                "sources": dict(sorted(state_counts.items())),
            }, separators=(",", ":")), flush=True)
            if result["failed"] and not result["updated"]:
                exit_code = 1
            if not args.watch:
                return exit_code
            await asyncio.sleep(args.interval)
    finally:
        await service.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
