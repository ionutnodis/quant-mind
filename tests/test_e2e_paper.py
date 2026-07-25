"""Opt-in E2E smoke test against a live IB Gateway paper session.

Run with:  uv run pytest -m e2e --override-ini addopts=''
Skipped in the default suite. This is the M1 definition-of-done path:
connect -> positions -> bars -> cache -> risk numbers.
"""

import numpy as np
import pytest

from quantmind.broker.connection import ConnectionManager
from quantmind.broker.ib_broker import IbBroker
from quantmind.config import Settings
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.risk.returns import historical_es, simple_returns

pytestmark = pytest.mark.e2e


async def test_connect_positions_bars_cache_risk(tmp_path):
    from ib_async import IB

    settings = Settings()
    ib = IB()
    mgr = ConnectionManager(
        ib, host=settings.host, port=settings.port, client_id=settings.client_id, max_attempts=2
    )
    await mgr.ensure_connected()
    assert ib.isConnected()

    broker = IbBroker(ib)
    portfolio = await broker.get_portfolio()
    print(f"\npositions: {len(portfolio.positions)}")

    # Use SPY as the guaranteed-resolvable instrument even on an empty paper book
    con_id = await broker.resolve_stock_con_id("SPY")
    bars = await broker.get_daily_bars(con_id, years=2)
    assert len(bars) > 250

    store = BarStore(tmp_path)
    store.write_bars(
        con_id=con_id,
        bar_size="1d",
        bars=bars,
        meta=BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof=str(bars.index[-1].date())),
    )
    cached, meta = store.read_bars(con_id=con_id, bar_size="1d")
    assert meta.bar_type == "ADJUSTED_LAST"

    returns = simple_returns(cached["close"])
    es = historical_es(returns, confidence=0.975)
    assert np.isfinite(es) and 0 < es < 0.2  # a sane daily ES for a broad index
    print(f"SPY 97.5% daily ES over 2y: {es:.4%}")

    ib.disconnect()
