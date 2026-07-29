"""Pure FX conversion core (FX-aware valuation, TODOS 2026-07-27).

Pure-core law: picklable pure functions/dataclasses, no I/O. Rates are
LOADED elsewhere (routers/_shared.load_fx_converter reads the cached
FX_{pair} series that sources/sync.sync_fx_bars writes); this module only
knows how IBKR names a pair and how to apply a rate.

Pair naming: IBKR (and the interbank market) order a pair by the standard
FX priority EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY — the
higher-priority currency comes first, and the quote is units of the SECOND
currency per 1 of the FIRST (GBPUSD 1.25 = 1.25 USD per 1 GBP). A currency
outside the priority list ranks below all the majors (USDSEK, not SEKUSD).

Honesty convention: a conversion with no cached rate returns None — never a
silently unconverted native amount (the exact bug this module exists to
close: totals that summed GBP and USD as if they were one currency).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

_PAIR_PRIORITY = ("EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY")


def _priority(currency: str) -> tuple[int, str]:
    try:
        rank = _PAIR_PRIORITY.index(currency)
    except ValueError:
        rank = len(_PAIR_PRIORITY)  # unlisted currencies rank below the majors
    return (rank, currency)


def fx_pair(currency: str, base: str) -> tuple[str, bool]:
    """(IBKR pair name, invert flag) such that
    rate(currency→base) = pair close if not invert else 1/close.

    currency == base is the CONVERTER's identity case, not a pair — there is
    no GBPGBP contract, so it raises rather than fabricating one."""
    if currency == base:
        raise ValueError(f"no FX pair for identical currencies ({currency!r})")
    first, second = sorted((currency, base), key=_priority)
    # The close quotes `second` per 1 `first`: direct when converting FROM
    # the pair's first currency, inverted when converting from the second.
    return f"{first}{second}", first == base


@dataclass(frozen=True)
class FxConverter:
    """Applies cached currency→base rates. `rates[cur]` is already oriented
    currency→base (the loader applies fx_pair's invert flag); `as_of` is the
    OLDEST of the loaded rates' last dates — the conservative staleness
    label a caller should surface next to any converted figure."""

    base: str
    rates: dict[str, float] = field(default_factory=dict)
    as_of: str | None = None

    def convert(self, value: float, currency: str) -> float | None:
        """`value` (in `currency`) expressed in `base`; identity for the
        base itself; honest None when no rate is cached — never a silently
        unconverted native amount."""
        if currency == self.base:
            return value
        rate = self.rates.get(currency)
        if rate is None:
            return None
        return value * rate

    def missing(self, currencies: Iterable[str]) -> set[str]:
        """The subset of `currencies` this converter cannot convert (the
        base itself never counts — identity needs no rate)."""
        return {c for c in currencies if c != self.base and c not in self.rates}
