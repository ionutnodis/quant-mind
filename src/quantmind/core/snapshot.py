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
    def create(
        cls, portfolio: Portfolio, valuation_ts: str, base_currency: str, extra: str = ""
    ) -> "BookSnapshot":
        """`extra` is an optional caller-supplied string folded into the hash
        (default "" reproduces the original portfolio-only identity exactly,
        so every existing caller is unaffected). book.py uses this to fold
        option-leg fields (strike/expiry/right) into the id. The original
        position identity is frozen for compatibility, so callers must fold
        newer contract terms into `extra`."""
        content = "|".join(
            f"{p.con_id}:{p.qty}:{p.multiplier}:{p.sec_type}" for p in sorted(
                portfolio.positions, key=lambda p: p.con_id
            )
        )
        digest = hashlib.sha256(
            f"{content}|{valuation_ts}|{base_currency}|{extra}".encode()
        ).hexdigest()[:12]
        return cls(
            portfolio=portfolio,
            valuation_ts=valuation_ts,
            base_currency=base_currency,
            snapshot_id=digest,
        )
