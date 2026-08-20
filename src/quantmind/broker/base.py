"""Broker interface (Engineering Constraints 3, 20 — and the design's premise 3).

The surface is only what the dashboard uses: positions, account values, adjusted
bars, option chains, and what-if margin. v1 is read-only, so execution methods
are intentionally absent from this interface.
"""

from __future__ import annotations

from abc import ABC

from quantmind.portfolio import Portfolio


class ReadOnlyBroker(ABC):
    def get_portfolio(self) -> Portfolio:  # pragma: no cover - interface default
        raise NotImplementedError
