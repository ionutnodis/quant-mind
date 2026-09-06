"""Isolated World UI demo for screenshots; never reads real accounts or feeds.

Run: uv run python scripts/world_demo.py
Start Vite with QM_API_PROXY_TARGET=http://127.0.0.1:8766, then visit /world.
All headlines are illustrative fixtures, not published investment information.
"""
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import httpx
import uvicorn

from quantmind.testing.synthetic_e2e import build_synthetic_app
from quantmind.world.models import WorldEvent, WorldProfile
from quantmind.world.sources import SOURCES

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def build(root: Path):
    app = build_synthetic_app(root)
    service = app.state.world
    service.clock = lambda: NOW
    service.transport = httpx.MockTransport(lambda _request: httpx.Response(503))
    service.cache.save_profile(WorldProfile(
        watch_symbols=["NVDA", "ASML"], interests=["semiconductors", "energy", "rates"],
        regions=["Europe", "US"],
    ))
    examples = [
        ("ecb", "Illustrative: ECB publishes monetary policy update",
         "Demo event: a central-bank release reaches a European investor's rates watch.", "11:30:00"),
        ("eia", "Illustrative: electricity demand and data-centre capacity",
         "Demo event: energy infrastructure is an explicit interest, not an inferred portfolio holding.", "10:45:00"),
        ("fed", "Illustrative: Federal Reserve releases banking update",
         "Demo event: US policy context appears with its original-source route and a separate timestamp.", "09:15:00"),
        ("un", "Illustrative: international shipping and trade briefing",
         "Demo event: broader world coverage remains visible in All even without a personal match.", "08:10:00"),
    ]
    for source_id, title, summary, time in examples:
        source = next(s for s in SOURCES if s.id == source_id)
        service.cache.record_success(source_id, [WorldEvent(
            id=f"demo-{source_id}", source_id=source_id, source_name=source.name,
            title=title, summary=summary, url=source.homepage,
            published_at=f"2026-09-05T{time}+00:00", topics=list(source.topics),
            regions=list(source.regions),
        )], NOW, source.interval_seconds)
    service.cache.record_failure("gdelt", "Source request timed out", NOW, 900)
    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="quantmind-world-demo-") as directory:
        uvicorn.run(build(Path(directory)), host="127.0.0.1", port=8766)
