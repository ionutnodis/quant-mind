"""Portfolio domain routes — the truth about the book (DESIGN.md: "the truth
about the book"). Thin wrapper over the tested pure core: `quantmind.portfolio`
for the Portfolio/Position types and `quantmind.core.snapshot` for the
identity (snapshot_id/valuation_ts) every risk result will key off later.

Serialization policy (repo-wide, api/app.py): UTC ISO-Z timestamps, NaN/Inf ->
null, missing/empty book -> structured empty, never a 500. Prices come from
the cached bar store only (no network call here) — a position with no cached
bars degrades to null price/market_value/weight, it never crashes the route.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import clean
from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio

router = APIRouter()


class PositionOut(BaseModel):
    con_id: int
    symbol: str
    qty: float
    sec_type: str
    multiplier: float
    last_close: float | None
    market_value: float | None
    weight: float | None


class Totals(BaseModel):
    market_value: float | None
    n_positions: int


class PortfolioResponse(BaseModel):
    snapshot_id: str
    valuation_ts: str
    base_currency: str
    positions: list[PositionOut]
    totals: Totals


def _last_close(store, con_id: int) -> float | None:
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        return None
    if bars.empty:
        return None
    return clean(float(bars["close"].iloc[-1]))


@router.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio(request: Request) -> PortfolioResponse:
    store = request.app.state.store
    broker = request.app.state.broker

    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if broker is None:
        portfolio = Portfolio(positions=(), as_of=valuation_ts)
    else:
        portfolio = await broker.get_portfolio()

    snapshot = BookSnapshot.create(portfolio, valuation_ts=valuation_ts, base_currency="USD")

    market_values: list[float | None] = []
    last_closes: list[float | None] = []
    for p in portfolio.positions:
        last_close = _last_close(store, p.con_id)
        last_closes.append(last_close)
        mv = clean(p.qty * p.multiplier * last_close) if last_close is not None else None
        market_values.append(mv)

    known_mvs = [mv for mv in market_values if mv is not None]
    total_mv = sum(known_mvs) if known_mvs else None

    positions_out = [
        PositionOut(
            con_id=p.con_id,
            symbol=p.symbol,
            qty=p.qty,
            sec_type=p.sec_type,
            multiplier=p.multiplier,
            last_close=last_close,
            market_value=mv,
            weight=(mv / total_mv if mv is not None and total_mv else None),
        )
        for p, last_close, mv in zip(portfolio.positions, last_closes, market_values)
    ]

    return PortfolioResponse(
        snapshot_id=snapshot.snapshot_id,
        valuation_ts=valuation_ts,
        base_currency="USD",
        positions=positions_out,
        totals=Totals(market_value=total_mv, n_positions=len(portfolio.positions)),
    )
