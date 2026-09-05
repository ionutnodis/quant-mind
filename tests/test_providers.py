"""Tests for the yfinance DataProvider (Task A2). Network calls are
isolated behind an injectable fetcher (pattern: quantmind.sources.fred) — no
test here ever hits the network."""

from __future__ import annotations

import pandas as pd
import pytest

from quantmind.sources.providers.yfinance_provider import YFinanceProvider


def _fake_history(symbol: str) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-05", periods=5, tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "High": [101.0, 102.0, 103.0, 104.0, 105.0],
            "Low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        },
        index=idx,
    )


def test_daily_bars_normalizes_columns_and_strips_timezone():
    provider = YFinanceProvider(fetcher=_fake_history)
    bars = provider.daily_bars("VXUS")
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars["close"].iloc[0] == 100.5
    assert bars.index.tz is None
    assert len(bars) == 5


def test_daily_series_returns_close_only():
    provider = YFinanceProvider(fetcher=_fake_history)
    series = provider.daily_series("VXUS")
    assert isinstance(series, pd.Series)
    assert series.iloc[-1] == 104.5


def test_provider_name_is_yfinance():
    assert YFinanceProvider().name == "yfinance"


def test_quote_currency_is_required_and_normalized():
    provider = YFinanceProvider(
        fetcher=_fake_history,
        currency_fetcher=lambda _symbol: " eur ",
    )

    assert provider.quote_currency("IWDA.AS") == "EUR"


@pytest.mark.parametrize("unit", ["GBp", "GBX"])
def test_london_pence_quote_convention_requires_price_scaling(unit):
    provider = YFinanceProvider(
        fetcher=_fake_history,
        currency_fetcher=lambda _symbol: unit,
    )

    assert provider.quote_convention("LGEN.L") == ("GBP", unit, 0.01)


def test_invalid_quote_currency_fails_closed():
    provider = YFinanceProvider(
        fetcher=_fake_history,
        currency_fetcher=lambda _symbol: "EURO",
    )

    with pytest.raises(ValueError, match="invalid quote currency"):
        provider.quote_currency("IWDA.AS")


def _empty_history(symbol: str) -> pd.DataFrame:
    return pd.DataFrame()


def test_missing_columns_raise_value_error_not_crash_downstream():
    def bad_fetcher(symbol):
        return pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.bdate_range("2026-01-05", periods=2))

    provider = YFinanceProvider(fetcher=bad_fetcher)
    with pytest.raises(ValueError, match="missing columns"):
        provider.daily_bars("BAD")


def test_fetcher_raising_lookup_error_propagates(monkeypatch):
    def failing_fetcher(symbol):
        raise LookupError(f"yfinance returned no history for {symbol!r}")

    provider = YFinanceProvider(fetcher=failing_fetcher)
    with pytest.raises(LookupError):
        provider.daily_bars("NOPE")
