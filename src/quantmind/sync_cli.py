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
from quantmind.fx import fx_pair
from quantmind.sources.providers.yfinance_provider import YFinanceProvider
from quantmind.sources.sync import (
    sync_book_bars,
    sync_daily_bars,
    sync_fx_bars,
    sync_index_bars,
    sync_instrument_metadata,
    sync_yfinance_bars,
)

# World-ETF region tags (Task A2: "wider world") — region metadata cached at
# sync alongside contract details; SH is a negative-beta validation
# instrument, tagged by book rather than geography.
WORLD_ETF_REGIONS = {
    "EZU": "Eurozone",
    "EWU": "United Kingdom",
    "EWY": "South Korea",
    "EWT": "Taiwan",
    "INDA": "India",
    "MCHI": "China",
    "EWZ": "Brazil",
    "EEM": "Emerging Markets",
    "EFA": "Developed ex-US",
    "SH": "US (inverse — negative-beta validation)",
}

# v1 macro-tile universe (design: hedge candidate list is config-editable; this
# is the starter set — crude via USO and DXY via UUP are the constraint-16 proxies)
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "USO", "UUP", "EWJ", "FXI", "EWG",
    # sectors
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    # factor proxies
    "MTUM", "VLUE", "QUAL", "USMV",
    # wider world (Task A2)
    *WORLD_ETF_REGIONS,
]

# VIX + SPX via IBKR Index contracts (empirically verified working: Task A2).
INDEX_UNIVERSE = {"VIX": "CBOE", "SPX": "CBOE"}


def needed_fx_pairs(store: BarStore, base_currency: str) -> list[str]:
    """IBKR pair names needed to value the STORED universe in
    `base_currency` (FX-aware valuation, TODOS 2026-07-27): the distinct
    metadata currencies of all cached instruments vs the base, deduped
    through fx_pair's naming (GBP+USD book needs exactly GBPUSD whichever
    of the two is base). Metadata without a currency can't vouch for one
    and is skipped; a single-currency store needs no pairs at all."""
    currencies = {
        cur
        for md in store.read_all_instrument_metadata().values()
        if (cur := md.get("currency"))
    }
    return sorted({fx_pair(cur, base_currency)[0] for cur in currencies if cur != base_currency})


async def sync_fx(store: BarStore, broker: IbBroker, base_currency: str) -> None:
    """Shared FX step for BOTH sync modes (default universe and --book):
    derive the needed pairs from stored metadata and sync their midpoint
    closes. No non-base currencies -> honest no-op."""
    pairs = needed_fx_pairs(store, base_currency)
    if not pairs:
        return
    written = await sync_fx_bars(store, broker, pairs, years=5, pace_seconds=2.0)
    for pair, last in written.items():
        print(f"FX_{pair} series through {last} (fx, base {base_currency})")


async def sync_book(store: BarStore, broker: IbBroker) -> None:
    """`--book` mode: sync the LIVE account's stock positions by their own
    conId (2026-07-27 incident: ticker-based resolution grabs the wrong
    listing for non-US instruments — e.g. LSE-listed UCITS ETFs sharing a
    US ticker). The held instrument wins the symbol-map entry."""
    portfolio = await broker.get_portfolio()
    synced = await sync_book_bars(store, broker, portfolio.positions, years=5, pace_seconds=2.0)
    for symbol, con_id in synced.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>6} conId={con_id:<12} bars through {wm.date()} (book)")
    skipped = sorted({p.symbol for p in portfolio.positions if p.sec_type != "STK"})
    if skipped:
        print(f"option legs skipped (chain ingestion owns those): {skipped}")
    await sync_instrument_metadata(store, broker, synced, pace_seconds=1.0)


async def main(symbols: list[str]) -> None:
    settings = Settings()
    store = BarStore(settings.data_dir)
    ib = IB()
    mgr = ConnectionManager(
        ib, host=settings.host, port=settings.port, client_id=settings.client_id, max_attempts=3
    )
    await mgr.ensure_connected()
    broker = IbBroker(ib)

    if symbols == ["--book"]:
        await sync_book(store, broker)
        # FX pairs derive from the freshly-written book metadata (a GBP-based
        # account holding USD names needs GBPUSD before totals can be honest).
        await sync_fx(store, broker, settings.base_currency)
        ib.disconnect()
        return

    symbol_map = await sync_daily_bars(store, broker, symbols, years=5, pace_seconds=2.0)
    for symbol, con_id in symbol_map.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>5} conId={con_id:<12} bars through {wm.date()}")

    index_map = await sync_index_bars(store, broker, INDEX_UNIVERSE, years=5, pace_seconds=2.0)
    for symbol, con_id in index_map.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>5} conId={con_id:<12} bars through {wm.date()} (index)")

    extra_tags = {sym: {"region": region} for sym, region in WORLD_ETF_REGIONS.items()}
    ibkr_map = {**symbol_map, **index_map}
    await sync_instrument_metadata(store, broker, ibkr_map, extra_tags=extra_tags, pace_seconds=1.0)
    # FX after metadata (currencies just landed), before disconnect — the
    # same shared step --book mode runs.
    await sync_fx(store, broker, settings.base_currency)
    ib.disconnect()

    yfinance_symbols = settings.yfinance_symbol_list()
    if yfinance_symbols:
        yf_map, skipped = sync_yfinance_bars(store, YFinanceProvider(), yfinance_symbols, years=5)
        for symbol in skipped:
            print(
                f"WARNING: {symbol} is IBKR-synced (positive conId) — skipped yfinance sync; "
                f"remove it from QM_YFINANCE_SYMBOLS (single-provenance law: IBKR wins)"
            )
        for symbol, con_id in yf_map.items():
            if symbol not in skipped:
                print(f"{symbol:>5} conId={con_id:<12} synced via yfinance")

    from quantmind.sources.fred import sync_fred

    for name, last in sync_fred(store).items():
        print(f"{name:>14} series through {last}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_UNIVERSE))
