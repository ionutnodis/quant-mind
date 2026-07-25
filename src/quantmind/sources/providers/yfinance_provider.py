"""yfinance-backed DataProvider (Task A2): the free fallback behind IBKR,
used only for symbols on the explicit config allowlist (Global Constraints:
free-first data, single-provenance law — this provider's name is recorded in
instrument metadata for every symbol it serves).

Network calls are isolated behind an injectable `fetcher` (pattern:
`quantmind.sources.fred`), so tests never hit the network — they supply a
fake fetcher returning a canned OHLCV frame.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

_Fetcher = Callable[[str], pd.DataFrame]

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def fetch_yfinance_history(symbol: str, period: str = "5y") -> pd.DataFrame:  # pragma: no cover - network
    """Default fetcher: yfinance's Ticker.history with auto_adjust=True, so
    the OHLC this returns is split/dividend-adjusted — consistent with the
    ADJUSTED_LAST law even though yfinance isn't IBKR (Engineering
    Constraint 3)."""
    import yfinance as yf

    df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    if df is None or df.empty:
        raise LookupError(f"yfinance returned no history for {symbol!r}")
    return df


class YFinanceProvider:
    """Implements `providers.base.DataProvider`."""

    name = "yfinance"

    def __init__(self, fetcher: _Fetcher = fetch_yfinance_history):
        self._fetcher = fetcher

    def daily_bars(self, symbol: str) -> pd.DataFrame:
        df = self._fetcher(symbol)
        df = df.rename(columns=str.lower)
        missing = set(_OHLCV_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"yfinance history for {symbol!r} missing columns {sorted(missing)}")
        out = df[_OHLCV_COLUMNS].astype(float)
        out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).tz_localize(None)
        out = out.sort_index()
        out.index.name = None
        return out

    def daily_series(self, name: str) -> pd.Series:
        return self.daily_bars(name)["close"]
