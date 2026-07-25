"""Book snapshot identity (Phase Plan / Codex #4): every risk result is keyed to
WHICH book it was computed against — positions content + valuation timestamp +
base currency, hashed into a stable snapshot_id used in cache keys."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from quantmind.portfolio import Portfolio


@dataclass(frozen=True)
class BookSnapshot:
    portfolio: Portfolio
    valuation_ts: str  # UTC ISO-8601, Z-suffixed
    base_currency: str
    snapshot_id: str

    @classmethod
    def create(cls, portfolio: Portfolio, valuation_ts: str, base_currency: str) -> "BookSnapshot":
        content = "|".join(
            f"{p.con_id}:{p.qty}:{p.multiplier}:{p.sec_type}" for p in sorted(
                portfolio.positions, key=lambda p: p.con_id
            )
        )
        digest = hashlib.sha256(
            f"{content}|{valuation_ts}|{base_currency}".encode()
        ).hexdigest()[:12]
        return cls(
            portfolio=portfolio,
            valuation_ts=valuation_ts,
            base_currency=base_currency,
            snapshot_id=digest,
        )
