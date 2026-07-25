"""One-shot cache sync: `uv run python -m quantmind.sync_cli [SYMBOLS...]`

Connects to IB Gateway, syncs adjusted daily bars for the universe into the
parquet store, and exits. This process is the designated datastore writer
while it runs (Engineering Constraint 4).
"""

from __future__ import annotations

import asyncio
import sys

from ib_async import IB

from quantmind.broker.connection import ConnectionManager
from quantmind.broker.ib_broker import IbBroker
from quantmind.config import Settings
from quantmind.datastore.store import BarStore
from quantmind.sources.sync import sync_daily_bars

# v1 macro-tile universe (design: hedge candidate list is config-editable; this
# is the starter set — crude via USO and DXY via UUP are the constraint-16 proxies)
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "USO", "UUP", "EWJ", "FXI", "EWG",
    # sectors
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    # factor proxies
    "MTUM", "VLUE", "QUAL", "USMV",
]


async def main(symbols: list[str]) -> None:
    settings = Settings()
    store = BarStore(settings.data_dir)
    ib = IB()
    mgr = ConnectionManager(
        ib, host=settings.host, port=settings.port, client_id=settings.client_id, max_attempts=3
    )
    await mgr.ensure_connected()
    broker = IbBroker(ib)
    symbol_map = await sync_daily_bars(store, broker, symbols, years=5, pace_seconds=2.0)
    for symbol, con_id in symbol_map.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>5} conId={con_id:<12} bars through {wm.date()}")
    ib.disconnect()
    from quantmind.sources.fred import sync_fred

    for name, last in sync_fred(store).items():
        print(f"{name:>14} series through {last}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_UNIVERSE))
