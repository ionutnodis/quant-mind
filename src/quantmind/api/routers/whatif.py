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

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.api.routers._shared import (
    PositionIn,
    clean,
    iso,
    read_close_series,
    refuse_unsupported_contract_legs,
    weighted_portfolio_returns,
)
from quantmind.api.routers.book import read_book_positions
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
_MAX_POSITIONS = 50


class MonteCarloParams(BaseModel):
    horizon: int = Field(126, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = None


class WhatIfRequest(BaseModel):
    # Exactly one of `positions` (inline book) or `book_ref` (a pinned
    # snapshot id from POST /api/book/pin or GET /api/book/current, wave-3
    # Task A1's book-flow spine) must be given — see `_positions_xor_book_ref`
    # below. Bounds on `positions` (1..50) are enforced here when given
    # inline; a book_ref-resolved book is bounds-checked in the handler
    # instead, since Field validators don't run on values assembled after
    # request parsing.
    positions: list[PositionIn] | None = Field(None, min_length=1, max_length=50)
    book_ref: str | None = None
    years: int = Field(5, ge=1, le=25)
    mc: MonteCarloParams = Field(default_factory=MonteCarloParams)

    @model_validator(mode="after")
    def _positions_xor_book_ref(self) -> "WhatIfRequest":
        if bool(self.positions) == bool(self.book_ref):
            raise ValueError("provide exactly one of positions or book_ref")
        return self


class Histogram(BaseModel):
    bin_edges: list[float]
    counts: list[int]


class WeightOut(BaseModel):
    # price/market_value/weight are nullable + _clean-wrapped (portfolio.py
    # precedent) as serialization defense-in-depth: no NaN/Inf may ever reach
    # the JSON. In practice a book leg with a non-finite last close is
    # rejected earlier with a named 422 (see the guard in whatif()) — unlike
    # GET /api/portfolio, a display of broker truth where a leg can degrade
    # to null fields, What-If's weights ARE the risk computation, and
    # silently dropping a leg would compute risk for a different book than
    # the one the user built.
    symbol: str
    qty: float
    price: float | None
    market_value: float | None
    weight: float | None


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


@router.post("/whatif", response_model=WhatIfResponse)
def whatif(request: Request, req: WhatIfRequest) -> WhatIfResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    symbol_map = store.read_symbol_map()

    # book_ref resolves to the same PositionIn shape as an inline book
    # (read_book_positions 422s naming the ref if it's unknown); Field's
    # min_length/max_length=1..50 only runs on an inline `positions` body, so
    # a book_ref-resolved list gets the same bounds check by hand here.
    positions = req.positions if req.positions is not None else read_book_positions(store, req.book_ref)
    if not positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(positions) > _MAX_POSITIONS:
        raise HTTPException(422, detail=f"book has {len(positions)} positions; max {_MAX_POSITIONS}")
    refuse_unsupported_contract_legs(positions, route_name="What-If")

    requested = [p.symbol for p in positions]
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
        series_map[sym] = read_close_series(store, symbol_map[sym], sym, req.years)

    # NaN/Inf last close (corrupted/partial sync data) makes a leg
    # unpriceable, and every downstream number (weights -> beta/ES/vol/MC)
    # keys off these prices. Reject with a named 422 rather than letting a
    # NaN propagate (never-crash / NaN-never-serialized policy).
    last_close = {sym: clean(float(s.iloc[-1])) for sym, s in series_map.items()}
    unpriceable = sorted(sym for sym in unique_needed if last_close[sym] is None)
    if unpriceable:
        raise HTTPException(
            422,
            detail=(
                f"non-finite last close in cached bars for: {unpriceable} — "
                "re-sync before computing"
            ),
        )

    market_values = [p.qty * last_close[p.symbol] for p in positions]
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
    # aligned column, which is correct — their weights simply add; see
    # _shared.weighted_portfolio_returns).
    weights_arr = np.array(weight_values)
    portfolio_returns = weighted_portfolio_returns(returns, [p.symbol for p in positions], weights_arr)

    try:
        beta_series = rolling_beta(portfolio_returns, bench_returns, window=_BETA_WINDOW, rf=0.0)
        beta_valid = beta_series.dropna()
        beta = clean(float(beta_valid.iloc[-1])) if len(beta_valid) else None
    except InsufficientDataError:
        beta = None

    try:
        es = clean(historical_es(portfolio_returns, confidence=0.975))
    except InsufficientDataError:
        es = None

    try:
        vol = clean(annualized_vol(portfolio_returns))
    except InsufficientDataError:
        vol = None

    try:
        bench_es = clean(historical_es(bench_returns, confidence=0.975))
    except InsufficientDataError:
        bench_es = None

    try:
        bench_vol = clean(annualized_vol(bench_returns))
    except InsufficientDataError:
        bench_vol = None

    # Monte Carlo terminal distribution over the SAME aligned per-leg returns
    # + weights (block bootstrap preserves cross-asset correlation), same
    # finite-guard shape as routers/risk.py and routers/lab.py.
    mc_returns_df = pd.DataFrame(
        {f"pos{i}": returns[p.symbol] for i, p in enumerate(positions)}, index=returns.index
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
            price=clean(last_close[p.symbol]),
            market_value=clean(market_values[i]),
            weight=clean(weight_values[i]),
        )
        for i, p in enumerate(positions)
    ]

    return WhatIfResponse(
        weights=weights_out,
        beta=beta,
        es_975=es,
        ann_vol=vol,
        mc=MonteCarloOut(
            histogram=Histogram(bin_edges=[float(e) for e in edges], counts=[int(c) for c in counts]),
            p5=clean(p5),
            p50=clean(p50),
            p95=clean(p95),
            n_nonfinite=n_nonfinite,
        ),
        benchmark=BenchmarkOut(symbol=benchmark, es_975=bench_es, ann_vol=bench_vol),
        n_obs=len(returns),
        as_of=iso(prices.index[-1]) if len(prices) else None,
    )
