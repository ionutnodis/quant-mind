"""Incremental, pacing-aware bar sync (Engineering Constraint 6).

First sync pulls full history; later syncs fetch only ~the window since the
per-conId watermark and merge (new data wins on overlap — adjusted bars can
rewrite recent history). A pacing sleep between instruments respects IBKR's
historical-data limits.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd

from quantmind.datastore.store import BarMeta, BarStore


def merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union by date, sorted; on overlapping dates the NEW bars win."""
    keep = existing.loc[~existing.index.isin(new.index)]
    return pd.concat([keep, new]).sort_index()


async def sync_daily_bars(
    store: BarStore,
    broker,
    symbols: list[str],
    years: int = 5,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
) -> dict[str, int]:
    """Sync adjusted daily bars for `symbols`; returns and persists symbol -> conId."""
    symbol_map: dict[str, int] = {}
    for symbol in symbols:
        con_id = await broker.resolve_stock_con_id(symbol)
        symbol_map[symbol] = con_id

        watermark = store.watermark(con_id=con_id, bar_size="1d")
        fetch_years = years if watermark is None else 1  # incremental: recent window only
        new = await broker.get_daily_bars(con_id, years=fetch_years)

        if watermark is not None:
            existing, _ = store.read_bars(con_id=con_id, bar_size="1d")
            new = merge_bars(existing, new)

        store.write_bars(
            con_id=con_id,
            bar_size="1d",
            bars=new,
            meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())),
        )
        await sleep(pace_seconds)

    store.write_symbol_map(symbol_map)
    return symbol_map
