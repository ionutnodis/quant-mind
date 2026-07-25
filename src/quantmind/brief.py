"""Morning-brief assembly: pure computation from the cache, no network.

Renders-from-cache is a hard constraint (Engineering Constraint / staleness
policy): this function must work with the Gateway down, and every consumer
shows `as_of` so staleness is visible, never hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from quantmind.analytics.correlation import correlation_matrix
from quantmind.datastore.store import BarStore
from quantmind.risk.returns import historical_es, simple_returns


@dataclass(frozen=True)
class Tile:
    symbol: str
    last_close: float
    change_1d: float


@dataclass(frozen=True)
class Brief:
    tiles: list[Tile] = field(default_factory=list)
    correlation: pd.DataFrame | None = None
    benchmark_es: float | None = None
    as_of: pd.Timestamp | None = None


def build_brief(store: BarStore, benchmark: str, es_confidence: float = 0.975) -> Brief:
    symbol_map = store.read_symbol_map()
    if not symbol_map:
        return Brief()

    closes: dict[str, pd.Series] = {}
    tiles: list[Tile] = []
    as_of: pd.Timestamp | None = None

    for symbol, con_id in symbol_map.items():
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
        close = bars["close"]
        closes[symbol] = close
        tiles.append(
            Tile(
                symbol=symbol,
                last_close=float(close.iloc[-1]),
                change_1d=float(close.iloc[-1] / close.iloc[-2] - 1.0),
            )
        )
        last = close.index[-1]
        as_of = last if as_of is None else max(as_of, last)

    returns = pd.DataFrame({s: simple_returns(c) for s, c in closes.items()}).dropna()
    corr = correlation_matrix(returns)

    benchmark_es = None
    if benchmark in closes:
        benchmark_es = historical_es(simple_returns(closes[benchmark]), confidence=es_confidence)

    return Brief(tiles=tiles, correlation=corr, benchmark_es=benchmark_es, as_of=as_of)
