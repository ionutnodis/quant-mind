"""The one Portfolio type (Engineering Constraint 9).

Live books (from the broker) and hypothetical books (from the what-if lab) are
structurally identical: positions keyed by IBKR conId + an as-of stamp. Every
risk/analytics/hedge function consumes this type and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd


@dataclass(frozen=True)
class Position:
    con_id: int
    symbol: str
    qty: float
    sec_type: str = "STK"
    multiplier: float = 1.0


@dataclass(frozen=True)
class Portfolio:
    positions: tuple[Position, ...]
    as_of: str

    def market_value(self, prices: Mapping[int, float]) -> float:
        return sum(p.qty * p.multiplier * self._price(prices, p.con_id) for p in self.positions)

    def weights(self, prices: Mapping[int, float]) -> pd.Series:
        """Market-value weights keyed by con_id. Empty portfolio -> empty series."""
        if not self.positions:
            return pd.Series(dtype=float)
        mv = {p.con_id: p.qty * p.multiplier * self._price(prices, p.con_id) for p in self.positions}
        total = sum(mv.values())
        return pd.Series({cid: v / total for cid, v in mv.items()})

    def with_position(self, position: Position) -> "Portfolio":
        """What-if: a new Portfolio with one more position; the original is untouched."""
        return Portfolio(positions=self.positions + (position,), as_of=self.as_of)

    @staticmethod
    def _price(prices: Mapping[int, float], con_id: int) -> float:
        try:
            return prices[con_id]
        except KeyError:
            raise KeyError(f"no price for con_id {con_id}") from None
