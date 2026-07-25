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


async def sync_index_bars(
    store: BarStore,
    broker,
    indices: dict[str, str],
    years: int = 5,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
) -> dict[str, int]:
    """Sync VIX/SPX-style index bars (Task A2: empirically verified working
    via IBKR Index contracts). `indices` maps symbol -> primary exchange
    (e.g. {"VIX": "CBOE", "SPX": "CBOE"}). Indices have no ADJUSTED_LAST feed
    (no splits/dividends), so the broker fetches TRADES bars instead — see
    IbBroker.get_index_bars.

    Merges resolved conIds into the EXISTING symbol map (read-modify-write)
    rather than overwriting it, since this may run in the same sync_cli pass
    as sync_daily_bars against a different symbol set."""
    symbol_map = store.read_symbol_map()
    for symbol, exchange in indices.items():
        con_id = await broker.resolve_index_con_id(symbol, exchange)
        symbol_map[symbol] = con_id

        watermark = store.watermark(con_id=con_id, bar_size="1d")
        fetch_years = years if watermark is None else 1
        new = await broker.get_index_bars(con_id, exchange, years=fetch_years)

        if watermark is not None:
            existing, _ = store.read_bars(con_id=con_id, bar_size="1d")
            new = merge_bars(existing, new)

        store.write_bars(
            con_id=con_id,
            bar_size="1d",
            bars=new,
            meta=BarMeta(bar_type="TRADES", adjusted_asof=str(date.today())),
        )
        await sleep(pace_seconds)

    store.write_symbol_map(symbol_map)
    return symbol_map


async def sync_instrument_metadata(
    store: BarStore,
    broker,
    symbol_map: dict[str, int],
    extra_tags: dict[str, dict] | None = None,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
) -> dict[str, dict]:
    """Contract-details metadata cache (Task A2): longName/exchange/currency/
    secType/industry per symbol, fetched from IBKR and merge-written into the
    store's instrument-metadata JSON. `extra_tags` layers in caller-supplied
    per-symbol fields (e.g. `region` for the world-ETF universe) — merged in
    after the fetched fields so a hand-picked tag always wins over a
    contract-details field of the same name."""
    extra_tags = extra_tags or {}
    written: dict[str, dict] = {}
    for symbol, con_id in symbol_map.items():
        details = await broker.fetch_contract_details(con_id)
        fields = {"con_id": con_id, "provider": "ibkr", **details, **extra_tags.get(symbol, {})}
        store.write_instrument_metadata(symbol, fields)
        written[symbol] = fields
        await sleep(pace_seconds)
    return written


def yfinance_pseudo_con_id(symbol: str) -> int:
    """Deterministic pseudo-conId for yfinance-sourced symbols. Always
    negative — real IBKR conIds are always positive — so this space can
    never collide with one (single-provenance law: each symbol maps to
    exactly one conId, which has exactly one provider tag in metadata)."""
    import hashlib

    digest = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    return -int(digest, 16)


def sync_yfinance_bars(
    store: BarStore,
    provider,
    symbols: list[str],
    years: int = 5,
) -> dict[str, int]:
    """Free-fallback sync (Task A2, Global Constraints: free-first data,
    single-provenance law) — for symbols on the explicit config allowlist
    that IBKR isn't the source for. Bars land in the same per-conId parquet
    layout as IBKR bars via a deterministic pseudo-conId, and the provider is
    recorded in instrument metadata so downstream consumers know the
    provenance. Merges into the existing symbol map rather than overwriting."""
    symbol_map = store.read_symbol_map()
    for symbol in symbols:
        con_id = yfinance_pseudo_con_id(symbol)
        bars = provider.daily_bars(symbol)
        if years > 0:
            bars = bars.iloc[-(years * 252):]
        store.write_bars(
            con_id=con_id,
            bar_size="1d",
            bars=bars,
            meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())),
        )
        store.write_instrument_metadata(symbol, {"con_id": con_id, "provider": provider.name})
        symbol_map[symbol] = con_id
    store.write_symbol_map(symbol_map)
    return symbol_map
