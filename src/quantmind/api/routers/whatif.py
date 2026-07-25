"""whatif domain routes: POST /api/whatif clones the book hypothetically and
recomputes risk side-by-side vs the live benchmark (DESIGN.md IA #5 — "clone
the book, modify, watch risk recompute"). Thin composition over the tested
pure core only (Global Constraints): `quantmind.risk.returns` for beta/ES/vol
and `quantmind.risk.montecarlo` for the block-bootstrap terminal distribution
— no math beyond wiring lives here.

Hypothetical books ARE the user's book for color purposes (wave-2 Global
Constraints addendum): the frontend renders these results in amber, this
router just supplies the honest numbers.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or insufficient overlap -> structured 422, never a 500 (pattern: routers/risk.py).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from quantmind.risk.montecarlo import simulate_terminal_returns
from quantmind.risk.returns import (
    InsufficientDataError,
    annualized_vol,
    historical_es,
    rolling_beta,
)

router = APIRouter()

_BETA_WINDOW = 60
_MAX_HIST_BINS = 60


def _clean(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf):
        return None
    return xf


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class PositionIn(BaseModel):
    symbol: str = Field(..., min_length=1)
    qty: float

    @field_validator("qty")
    @classmethod
    def _qty_nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("qty must be nonzero")
        return v


class MonteCarloParams(BaseModel):
    horizon: int = Field(126, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = None


class WhatIfRequest(BaseModel):
    positions: list[PositionIn] = Field(..., min_length=1, max_length=50)
    years: int = Field(5, ge=1, le=25)
    mc: MonteCarloParams = Field(default_factory=MonteCarloParams)


class Histogram(BaseModel):
    bin_edges: list[float]
    counts: list[int]


class WeightOut(BaseModel):
    symbol: str
    qty: float
    price: float
    market_value: float
    weight: float


class MonteCarloOut(BaseModel):
    histogram: Histogram
    p5: float | None
    p50: float | None
    p95: float | None
    # Paths dropped because their terminal return overflowed to non-finite
    # (pattern: routers/risk.py, routers/lab.py) — stats/histogram cover only
    # the finite paths; the response is honest about the rest.
    n_nonfinite: int


class BenchmarkOut(BaseModel):
    symbol: str
    es_975: float | None
    ann_vol: float | None


class WhatIfResponse(BaseModel):
    weights: list[WeightOut]
    beta: float | None
    es_975: float | None
    ann_vol: float | None
    mc: MonteCarloOut
    benchmark: BenchmarkOut
    n_obs: int
    as_of: str | None


def _read_close_series(request: Request, con_id: int, symbol: str, years: int) -> pd.Series:
    store = request.app.state.store
    try:
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
    except FileNotFoundError:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    series = bars["close"]
    if years > 0:
        series = series.iloc[-(years * 252) :]
    if series.empty:
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached history")
    return series


@router.post("/whatif", response_model=WhatIfResponse)
def whatif(request: Request, req: WhatIfRequest) -> WhatIfResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    symbol_map = store.read_symbol_map()

    requested = [p.symbol for p in req.positions]
    unique_needed = list(dict.fromkeys(requested))
    unknown = sorted(s for s in unique_needed if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")
    if benchmark not in symbol_map:
        raise HTTPException(422, detail=f"benchmark {benchmark!r} not in cache")

    # Inner-join every symbol involved (book legs + benchmark) on trading
    # dates: portfolio daily returns are only well-defined where every leg
    # (and the benchmark, for beta) has a price.
    series_map: dict[str, pd.Series] = {}
    for sym in [*unique_needed, benchmark]:
        if sym in series_map:
            continue
        series_map[sym] = _read_close_series(request, symbol_map[sym], sym, req.years)

    last_close = {sym: float(s.iloc[-1]) for sym, s in series_map.items()}

    market_values = [p.qty * last_close[p.symbol] for p in req.positions]
    gross = sum(abs(mv) for mv in market_values)
    if gross <= 0:
        raise HTTPException(422, detail="portfolio has zero gross market value")
    weight_values = [mv / gross for mv in market_values]

    prices = pd.concat(series_map, axis=1).dropna()
    if len(prices) < _BETA_WINDOW + 2:
        raise HTTPException(
            422,
            detail=(
                f"only {len(prices)} overlapping observations across book+benchmark; "
                f"need > window+1 ({_BETA_WINDOW + 1})"
            ),
        )

    returns = prices.pct_change().dropna()
    bench_returns = returns[benchmark]

    # Portfolio daily returns = weighted sum of each position's aligned
    # simple return (duplicate symbols across positions reuse the same
    # aligned column, which is correct — their weights simply add).
    position_returns = np.column_stack([returns[p.symbol].to_numpy() for p in req.positions])
    weights_arr = np.array(weight_values)
    portfolio_returns = pd.Series(position_returns @ weights_arr, index=returns.index)

    try:
        beta_series = rolling_beta(portfolio_returns, bench_returns, window=_BETA_WINDOW, rf=0.0)
        beta_valid = beta_series.dropna()
        beta = _clean(float(beta_valid.iloc[-1])) if len(beta_valid) else None
    except InsufficientDataError:
        beta = None

    try:
        es = _clean(historical_es(portfolio_returns, confidence=0.975))
    except InsufficientDataError:
        es = None

    try:
        vol = _clean(annualized_vol(portfolio_returns))
    except InsufficientDataError:
        vol = None

    try:
        bench_es = _clean(historical_es(bench_returns, confidence=0.975))
    except InsufficientDataError:
        bench_es = None

    try:
        bench_vol = _clean(annualized_vol(bench_returns))
    except InsufficientDataError:
        bench_vol = None

    # Monte Carlo terminal distribution over the SAME aligned per-leg returns
    # + weights (block bootstrap preserves cross-asset correlation), same
    # finite-guard shape as routers/risk.py and routers/lab.py.
    mc_returns_df = pd.DataFrame(
        {f"pos{i}": returns[p.symbol] for i, p in enumerate(req.positions)}, index=returns.index
    )
    terminal = simulate_terminal_returns(
        mc_returns_df,
        weights=weights_arr,
        n_paths=req.mc.n_paths,
        horizon=req.mc.horizon,
        seed=req.mc.seed,
    )
    finite_terminal = terminal[np.isfinite(terminal)]
    n_nonfinite = int(len(terminal) - len(finite_terminal))
    if len(finite_terminal) == 0:
        raise HTTPException(
            422,
            detail=(
                "simulation produced no finite terminal returns — check cached bars for "
                "zero/degenerate prices"
            ),
        )

    n_bins = min(_MAX_HIST_BINS, max(1, len(finite_terminal)))
    counts, edges = np.histogram(finite_terminal, bins=n_bins)
    p5, p50, p95 = (float(x) for x in np.percentile(finite_terminal, [5, 50, 95]))

    weights_out = [
        WeightOut(
            symbol=p.symbol,
            qty=p.qty,
            price=last_close[p.symbol],
            market_value=market_values[i],
            weight=weight_values[i],
        )
        for i, p in enumerate(req.positions)
    ]

    return WhatIfResponse(
        weights=weights_out,
        beta=beta,
        es_975=es,
        ann_vol=vol,
        mc=MonteCarloOut(
            histogram=Histogram(bin_edges=[float(e) for e in edges], counts=[int(c) for c in counts]),
            p5=_clean(p5),
            p50=_clean(p50),
            p95=_clean(p95),
            n_nonfinite=n_nonfinite,
        ),
        benchmark=BenchmarkOut(symbol=benchmark, es_975=bench_es, ann_vol=bench_vol),
        n_obs=len(returns),
        as_of=_iso(prices.index[-1]) if len(prices) else None,
    )
