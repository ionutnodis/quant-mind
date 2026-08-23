"""Serve the web E2E suite from an isolated, deterministic market-data cache."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import uvicorn

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore

_AS_OF = "2026-07-24"
_E2E_ORIGINS = ("http://localhost:4173", "http://127.0.0.1:4173")


def _bars(symbol: str) -> pd.DataFrame:
    index = pd.bdate_range(end=_AS_OF, periods=320)
    step = np.arange(len(index), dtype=float)
    market_returns = 0.00035 + 0.007 * np.sin(step / 8.0) + 0.003 * np.cos(step / 19.0)
    if symbol == "SPY":
        returns = market_returns
        start = 500.0
    else:
        returns = 1.2 * market_returns + 0.0015 * np.sin(step / 5.0)
        start = 430.0
    close = start * np.cumprod(1.0 + returns)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": 1_000_000.0 + step * 100.0,
        },
        index=index,
    )


def build_synthetic_store(root: Path) -> BarStore:
    store = BarStore(root)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=_AS_OF)
    store.write_bars(con_id=1, bar_size="1d", bars=_bars("SPY"), meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=_bars("QQQ"), meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    return store


def build_synthetic_app(root: Path):
    return create_app(
        store=build_synthetic_store(root),
        benchmark="SPY",
        allowed_origins=_E2E_ORIGINS,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="quantmind-e2e-") as tmp:
        uvicorn.run(build_synthetic_app(Path(tmp)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
