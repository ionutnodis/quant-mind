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
_CurrencyFetcher = Callable[[str], str]

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


def fetch_yfinance_currency(symbol: str) -> str:  # pragma: no cover - network
    """Fetch the listing's quote currency from yfinance instrument metadata."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    fast_info = ticker.fast_info
    value = (
        fast_info.get("currency")
        if hasattr(fast_info, "get")
        else getattr(fast_info, "currency", None)
    )
    if not value:
        value = (getattr(ticker, "history_metadata", None) or {}).get("currency")
    if not value:
        raise LookupError(f"yfinance returned no quote currency for {symbol!r}")
    return str(value)


class YFinanceProvider:
    """Implements `providers.base.DataProvider`."""

    name = "yfinance"

    def __init__(
        self,
        fetcher: _Fetcher = fetch_yfinance_history,
        currency_fetcher: _CurrencyFetcher = fetch_yfinance_currency,
    ):
        self._fetcher = fetcher
        self._currency_fetcher = currency_fetcher

    def quote_convention(self, symbol: str) -> tuple[str, str, float]:
        """Return (ISO currency, source quote unit, price-to-currency scale).

        Yahoo uses ``GBp``/``GBX`` for London prices quoted in pence. Those
        bars must be divided by 100 before any GBP FX or portfolio math.
        """
        quote_unit = str(self._currency_fetcher(symbol) or "").strip()
        if quote_unit == "GBp" or quote_unit.upper() == "GBX":
            return "GBP", quote_unit, 0.01
        currency = quote_unit.upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError(
                f"yfinance returned invalid quote currency {currency!r} for {symbol!r}"
            )
        return currency, quote_unit, 1.0

    def quote_currency(self, symbol: str) -> str:
        return self.quote_convention(symbol)[0]

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
