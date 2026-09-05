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


class InstrumentIdentityConflictError(ValueError):
    """A ticker resolves to multiple broker-authoritative contracts."""


def merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union by date, sorted; on overlapping dates the NEW bars win."""
    keep = existing.loc[~existing.index.isin(new.index)]
    return pd.concat([keep, new]).sort_index()


def _positive_con_id(value: object, *, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{description} is not a valid positive conId")
    return value


async def _resolve_daily_con_id(
    broker,
    symbol: str,
    *,
    known_con_id: int | list[int] | None,
    option_contract_con_ids: list[int] | None,
) -> int:
    """Resolve one listing without discarding held derivative identity."""

    authoritative: set[int] = set()
    if known_con_id is not None:
        stock_con_ids = (
            known_con_id if isinstance(known_con_id, list) else [known_con_id]
        )
        if not stock_con_ids:
            raise LookupError(f"held stocks for {symbol!r} have no contract identities")
        for stock_con_id in stock_con_ids:
            authoritative.add(
                _positive_con_id(
                    stock_con_id,
                    description=f"held stock {symbol!r}",
                )
            )
    if option_contract_con_ids is not None:
        if not option_contract_con_ids:
            raise LookupError(
                f"held options for {symbol!r} have no contract identities"
            )
        for option_con_id in dict.fromkeys(option_contract_con_ids):
            option_id = _positive_con_id(
                option_con_id,
                description=f"held option for {symbol!r}",
            )
            underlying_con_id = await broker.resolve_option_underlying_con_id(
                option_id
            )
            authoritative.add(
                _positive_con_id(
                    underlying_con_id,
                    description=f"underlying for held option {option_id}",
                )
            )
    if len(authoritative) > 1:
        raise InstrumentIdentityConflictError(
            f"conflicting authoritative underlying conIds for {symbol!r}: "
            f"{sorted(authoritative)}"
        )
    if authoritative:
        return next(iter(authoritative))
    return await broker.resolve_stock_con_id(symbol)


async def sync_daily_bars(
    store: BarStore,
    broker,
    symbols: list[str],
    years: int = 5,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
    known_con_ids: dict[str, int | list[int]] | None = None,
    option_contract_con_ids: dict[str, list[int]] | None = None,
    failures: dict[str, str] | None = None,
) -> dict[str, int]:
    """Sync adjusted daily bars for `symbols`; returns and persists symbol -> conId.

    Read-modify-writes the symbol map (same discipline as sync_index_bars /
    sync_yfinance_bars): a partial-universe run (`python -m quantmind.sync_cli
    SPY`) must never wipe mappings written by other sync passes (indices,
    world ETFs, yfinance entries). Persists the MERGED map but returns only
    the symbols synced in THIS call, so callers (sync_cli's watermark print +
    metadata fetch) don't accidentally treat other providers' symbols as
    IBKR-synced. Held option contract IDs are authoritative: their IBKR
    ``underConId`` values must resolve and agree with every held stock identity
    for the same symbol; the ticker-based USD resolver is used only when no
    held contract supplies identity."""
    persisted_map = store.read_symbol_map()
    known_con_ids = known_con_ids or {}
    option_contract_con_ids = option_contract_con_ids or {}
    symbol_map: dict[str, int] = {}
    for symbol in symbols:
        resolved_con_id: int | None = None
        try:
            resolved_con_id = await _resolve_daily_con_id(
                broker,
                symbol,
                known_con_id=known_con_ids.get(symbol),
                option_contract_con_ids=(
                    option_contract_con_ids[symbol]
                    if symbol in option_contract_con_ids
                    else None
                ),
            )
            watermark = store.watermark(con_id=resolved_con_id, bar_size="1d")
            fetch_years = years if watermark is None else 1
            new = await broker.get_daily_bars(resolved_con_id, years=fetch_years)

            if watermark is not None:
                existing, _ = store.read_bars(
                    con_id=resolved_con_id, bar_size="1d"
                )
                new = merge_bars(existing, new)

            store.write_bars(
                con_id=resolved_con_id,
                bar_size="1d",
                bars=new,
                meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())),
            )
            symbol_map[symbol] = resolved_con_id
            persisted_map[symbol] = resolved_con_id
        except InstrumentIdentityConflictError as exc:
            # A cached symbol mapping is unsafe once the live book proves that
            # the ticker names more than one contract. Keep the conId-addressed
            # bars for audit/recovery, but remove the ambiguous ticker pointer
            # so Setup and every analytical route fail closed until identity is
            # resolved by a subsequent successful sync.
            persisted_map.pop(symbol, None)
            store.write_symbol_map(persisted_map)
            if failures is None:
                raise
            failures[symbol] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            if (
                resolved_con_id is not None
                and persisted_map.get(symbol) not in {None, resolved_con_id}
            ):
                # The broker has established that the cached ticker points at
                # a different contract. If the replacement bars fail, remove
                # the known-wrong pointer instead of serving the prior listing.
                persisted_map.pop(symbol, None)
                store.write_symbol_map(persisted_map)
            if failures is None:
                raise
            failures[symbol] = f"{type(exc).__name__}: {exc}"
        await sleep(pace_seconds)

    store.write_symbol_map(persisted_map)
    return symbol_map


async def sync_index_bars(
    store: BarStore,
    broker,
    indices: dict[str, str],
    years: int = 5,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
    failures: dict[str, str] | None = None,
) -> dict[str, int]:
    """Sync VIX/SPX-style index bars (Task A2: empirically verified working
    via IBKR Index contracts). `indices` maps symbol -> primary exchange
    (e.g. {"VIX": "CBOE", "SPX": "CBOE"}). Indices have no ADJUSTED_LAST feed
    (no splits/dividends), so the broker fetches TRADES bars instead — see
    IbBroker.get_index_bars.

    Merges resolved conIds into the EXISTING symbol map (read-modify-write)
    rather than overwriting it, since this may run in the same sync_cli pass
    as sync_daily_bars against a different symbol set. Persists the MERGED
    map but returns only the indices synced in THIS call (see
    sync_daily_bars' rationale)."""
    persisted_map = store.read_symbol_map()
    symbol_map: dict[str, int] = {}
    for symbol, exchange in indices.items():
        try:
            con_id = await broker.resolve_index_con_id(symbol, exchange)
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
            symbol_map[symbol] = con_id
            persisted_map[symbol] = con_id
        except Exception as exc:
            if failures is None:
                raise
            failures[symbol] = f"{type(exc).__name__}: {exc}"
        await sleep(pace_seconds)

    store.write_symbol_map(persisted_map)
    return symbol_map


async def sync_instrument_metadata(
    store: BarStore,
    broker,
    symbol_map: dict[str, int],
    extra_tags: dict[str, dict] | None = None,
    sleep=asyncio.sleep,
    pace_seconds: float = 0.5,
    failures: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Contract-details metadata cache (Task A2): longName/exchange/currency/
    secType/industry per symbol, fetched from IBKR and merge-written into the
    store's instrument-metadata JSON. `extra_tags` layers in caller-supplied
    per-symbol fields (e.g. `region` for the world-ETF universe) — merged in
    after the fetched fields so a hand-picked tag always wins over a
    contract-details field of the same name."""
    extra_tags = extra_tags or {}
    written: dict[str, dict] = {}
    try:
        existing_metadata = store.read_all_instrument_metadata()
        rebuild_corrupt_cache = False
    except ValueError:
        # Salvage valid records before rebuilding so an unrelated malformed
        # entry plus a partial provider outage cannot erase good metadata.
        existing_metadata = store.read_recoverable_instrument_metadata()
        rebuild_corrupt_cache = True
    provider_identity_defaults = {
        "primary_exchange": None,
        "local_symbol": None,
        "trading_class": None,
        "stock_type": None,
        "issuer_id": None,
        "isin": None,
        "valid_exchanges": [],
        "external_identifiers": {},
    }
    for symbol, con_id in symbol_map.items():
        try:
            details = await broker.fetch_contract_details(con_id)
            previous = existing_metadata.get(symbol) or {}
            tags = extra_tags.get(symbol, {})
            fields = {
                **previous,
                "con_id": con_id,
                "provider": "ibkr",
                **provider_identity_defaults,
                **details,
                **tags,
            }
            if previous.get("con_id") == con_id:
                for identity_field in ("isin", "stock_type"):
                    if identity_field not in details and identity_field not in tags:
                        fields[identity_field] = previous.get(identity_field)
            identity_changed = (
                previous.get("con_id") not in {None, con_id}
                or previous.get("isin") != fields.get("isin")
                or previous.get("stock_type") != fields.get("stock_type")
            )
            if identity_changed:
                fields.update(
                    {
                        "ucits_profile_isin": None,
                        "ucits_profile_status": None,
                        "ucits_profile_reason": None,
                    }
                )
            if not rebuild_corrupt_cache:
                store.write_instrument_metadata(symbol, fields)
            written[symbol] = fields
        except Exception as exc:
            if failures is None:
                raise
            failures[symbol] = f"{type(exc).__name__}: {exc}"
        await sleep(pace_seconds)
    if rebuild_corrupt_cache:
        store.replace_instrument_metadata({**existing_metadata, **written})
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
    failures: dict[str, str] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Free-fallback sync (Task A2, Global Constraints: free-first data,
    single-provenance law) — for symbols on the explicit config allowlist
    that IBKR isn't the source for. Bars land in the same per-conId parquet
    layout as IBKR bars via a deterministic pseudo-conId, and the provider is
    recorded in instrument metadata so downstream consumers know the
    provenance. Merges into the existing symbol map rather than overwriting.

    Returns (symbol_map for THIS call's symbols, skipped): a symbol already
    mapped to a POSITIVE conId is IBKR-sourced — silently repointing it to a
    pseudo-conId and flipping its provider would violate the
    single-provenance law and leave stale IBKR metadata fields lingering
    under a yfinance tag. IBKR provenance wins: the symbol is skipped
    untouched and named in `skipped` so the CLI can warn the operator to fix
    the allowlist."""
    persisted_map = store.read_symbol_map()
    symbol_map: dict[str, int] = {}
    skipped: list[str] = []
    for symbol in symbols:
        existing_con_id = persisted_map.get(symbol)
        if existing_con_id is not None and existing_con_id > 0:
            skipped.append(symbol)
            symbol_map[symbol] = existing_con_id  # report the (untouched) IBKR mapping
            continue
        try:
            con_id = yfinance_pseudo_con_id(symbol)
            if hasattr(provider, "quote_convention"):
                quote_currency, quote_unit, price_scale = provider.quote_convention(symbol)
            else:
                quote_currency = provider.quote_currency(symbol)
                quote_unit, price_scale = quote_currency, 1.0
            bars = provider.daily_bars(symbol)
            if price_scale != 1.0:
                bars = bars.copy()
                bars.loc[:, ["open", "high", "low", "close"]] *= price_scale
            if years > 0:
                bars = bars.iloc[-(years * 252):]
            store.write_bars(
                con_id=con_id,
                bar_size="1d",
                bars=bars,
                meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(date.today())),
            )
            store.write_instrument_metadata(
                symbol,
                {
                    "con_id": con_id,
                    "provider": provider.name,
                    "currency": quote_currency,
                    "quote_unit": quote_unit,
                    "price_scale": price_scale,
                },
            )
            symbol_map[symbol] = con_id
            persisted_map[symbol] = con_id
        except Exception as exc:
            if failures is None:
                raise
            failures[symbol] = f"{type(exc).__name__}: {exc}"
    store.write_symbol_map(persisted_map)
    return symbol_map, skipped
