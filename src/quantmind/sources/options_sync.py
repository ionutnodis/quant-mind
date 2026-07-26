"""Options chain sync (Task A3): for each configured underlier, fetch chain
params, select monthlies <= 90d out with strikes within +/-15% of spot, paced-
snapshot bid/ask/IV/delta, and persist via OptionsStore. Composition only —
all math lives in risk/options.py (consumed later by exposure/book_greeks.py),
all IB I/O lives in broker/ib_options.py; this module just wires the two
together against the store (pattern: sources/sync.py's sync_daily_bars).

Spot is read from the already-synced stock bars (BarStore), not a fresh live
snapshot: SPY/QQQ are in sync_cli.DEFAULT_UNIVERSE, so their adjusted daily
bars are cached well before an options sync ever runs, and last close is a
reasonable spot reference for the +/-15% strike band (a same-day intraday move
shifting the "right" band by a percent or two is an acceptable approximation
for a chain sync, not a pricing input).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Sequence

import pandas as pd

from quantmind.broker.ib_options import (
    OptionQuote,
    fetch_chain_params,
    select_monthly_expiries,
    select_strikes_near_spot,
    snapshot_option_quotes,
)
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.datastore.store import BarStore

_QUOTE_COLUMNS = ["expiry", "strike", "right", "con_id", "bid", "ask", "iv", "delta", "multiplier"]


def _quotes_to_frame(quotes: list[OptionQuote]) -> pd.DataFrame:
    if not quotes:
        return pd.DataFrame(columns=_QUOTE_COLUMNS)
    return pd.DataFrame(
        {
            "expiry": [q.expiry for q in quotes],
            "strike": [q.strike for q in quotes],
            "right": [q.right for q in quotes],
            "con_id": [q.con_id for q in quotes],
            "bid": [q.bid for q in quotes],
            "ask": [q.ask for q in quotes],
            "iv": [q.iv for q in quotes],
            "delta": [q.delta for q in quotes],
            "multiplier": [q.multiplier for q in quotes],
        }
    )


async def sync_options_chains(
    options_store: OptionsStore,
    bar_store: BarStore,
    ib,
    underliers: Sequence[str] = ("SPY", "QQQ"),
    as_of: date | None = None,
    max_days_to_expiry: int = 90,
    strike_pct: float = 0.15,
    sleep=asyncio.sleep,
    pace_seconds: float = 1.0,
    batch_size: int = 50,
) -> dict[str, int]:
    """Syncs one options chain snapshot per underlier; returns underlier ->
    number of contracts snapshotted. An underlier missing from the stock
    symbol map (bars never synced) raises LookupError — options ingestion
    depends on the stock sync having run first, not a separate spot fetch."""
    as_of = as_of or date.today()
    symbol_map = bar_store.read_symbol_map()
    counts: dict[str, int] = {}

    for underlier in underliers:
        if underlier not in symbol_map:
            raise LookupError(
                f"{underlier!r} not in the cached symbol map — sync its stock bars "
                "(quantmind.sync_cli) before syncing its option chain"
            )
        con_id = symbol_map[underlier]
        bars, _ = bar_store.read_bars(con_id=con_id, bar_size="1d")
        spot = float(bars["close"].iloc[-1])

        chain = await fetch_chain_params(ib, underlier, con_id)
        expiries = select_monthly_expiries(chain.expirations, as_of=as_of, max_days=max_days_to_expiry)
        strikes = select_strikes_near_spot(chain.strikes, spot=spot, pct=strike_pct)
        quotes = await snapshot_option_quotes(
            ib,
            chain,
            expiries=expiries,
            strikes=strikes,
            sleep=sleep,
            pace_seconds=pace_seconds,
            batch_size=batch_size,
        )
        df = _quotes_to_frame(quotes)
        options_store.write_chain(underlier, df, OptionsSnapshotMeta(as_of=str(as_of), spot=spot))
        counts[underlier] = len(df)
        await sleep(pace_seconds)  # pace between underliers, same discipline as sync_daily_bars

    return counts
