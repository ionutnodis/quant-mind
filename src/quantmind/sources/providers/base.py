"""Data-provider Protocol (Task A2, Global Constraints: single-provenance
law — a series/instrument's history comes from exactly one source, and that
source is recorded in instrument metadata). IBKR (`quantmind.broker.ib_broker`
+ `sources.sync`) is primary; anything implementing this Protocol is a
free-first fallback used ONLY for instruments/series IBKR can't serve, and
only behind an explicit config allowlist — never a silent substitute for an
IBKR failure.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class DataProvider(Protocol):
    name: str

    def daily_bars(self, symbol: str) -> pd.DataFrame:
        """OHLCV daily bars for `symbol`, indexed by date, columns
        open/high/low/close/volume (float) — the same shape the IBKR broker
        path produces, so the store/API can treat provider-sourced bars
        identically once written."""
        ...

    def daily_series(self, name: str) -> pd.Series:
        """A single named close-price series for `name` — parity with the
        store's named-series API (`BarStore.read_series`) for cases where a
        provider-sourced value isn't naturally an OHLC instrument."""
        ...
