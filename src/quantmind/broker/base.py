"""Broker interface (Engineering Constraints 3, 20 — and the design's premise 3).

The surface is only what the dashboard uses: positions, account values, adjusted
bars, option chains, what-if margin — and, later, orders. v1 is read-only:
`place_order` exists on the interface but is disabled.
"""

from __future__ import annotations

from abc import ABC

from quantmind.portfolio import Portfolio


class ExecutionDisabledError(RuntimeError):
    """v1 is read-only; execution arrives in a later phase behind this same interface."""


class ReadOnlyBroker(ABC):
    def get_portfolio(self) -> Portfolio:  # pragma: no cover - interface default
        raise NotImplementedError

    def place_order(self, order) -> None:
        raise ExecutionDisabledError(
            "Order execution is disabled in v1 (read-only). See design doc execution phase."
        )
