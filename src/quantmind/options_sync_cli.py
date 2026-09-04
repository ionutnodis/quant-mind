"""One-shot options chain sync: `uv run python -m quantmind.options_sync_cli [UNDERLIERS...]`

Connects to IB Gateway and snapshots monthlies <= 90d out, strikes within
+/-15% of spot, for the configured underliers (default SPY, QQQ) into the
options parquet store. Mirrors sync_cli.py's shape exactly, but is a SEPARATE
CLI the user runs manually — it is NOT wired into /api/sync or any app
startup path (wave-3 plan: live options ingestion is opt-in, same posture as
the stock sync_cli).

Depends on the stock sync having already run: spot is read from the cached
adjusted bars (BarStore), not fetched live (see sources/options_sync.py).
"""

from __future__ import annotations

import asyncio
import sys

from ib_async import IB

from quantmind.broker.connection import ConnectionManager
from quantmind.config import Settings
from quantmind.datastore.locking import exclusive_sync_lock
from quantmind.datastore.options_store import OptionsStore
from quantmind.datastore.store import BarStore
from quantmind.sources.options_sync import sync_options_chains

DEFAULT_UNDERLIERS = ["SPY", "QQQ"]


async def main(underliers: list[str]) -> None:
    settings = Settings()
    with exclusive_sync_lock(settings.data_dir):
        bar_store = BarStore(settings.data_dir)
        options_store = OptionsStore(settings.data_dir)
        ib = IB()
        try:
            mgr = ConnectionManager(
                ib,
                host=settings.host,
                port=settings.port,
                client_id=settings.client_id,
                max_attempts=3,
            )
            await mgr.ensure_connected()
            counts = await sync_options_chains(
                options_store,
                bar_store,
                ib,
                underliers=underliers,
                pace_seconds=1.0,
            )
            for underlier, n in counts.items():
                print(f"{underlier:>5} snapshotted {n} option contracts")
        finally:
            ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_UNDERLIERS))
