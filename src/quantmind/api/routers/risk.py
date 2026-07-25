"""Risk domain routes: thin wrappers over `quantmind.risk` (Global Constraints).

GET /risk/{symbol}: rolling beta/alpha vs `app.state.benchmark`, historical ES
97.5%, annualized vol — read straight from cached bars, never network.
POST /risk/montecarlo: block-bootstrap terminal-return distribution for a
single symbol via `quantmind.risk.montecarlo`.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbol or
insufficient history -> structured 422, never a 500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from quantmind.api.routers._shared import clean, downsample, iso
from quantmind.risk.montecarlo import simulate_terminal_returns
from quantmind.risk.returns import (
    InsufficientDataError,
    annualized_vol,
    historical_es,
    rolling_alpha,
    rolling_beta,
    simple_returns,
)

router = APIRouter()

_MAX_BETA_POINTS = 500
_MAX_HIST_BINS = 60


def _price_series(request: Request, symbol: str, years: int) -> pd.Series:
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    if symbol not in symbol_map:
        raise HTTPException(422, detail=f"symbol {symbol!r} not in cache")
    try:
        bars, _ = store.read_bars(con_id=symbol_map[symbol], bar_size="1d")
    except FileNotFoundError:
        # Mapped but never synced: missing data is a structured 422, never a 500.
        raise HTTPException(422, detail=f"symbol {symbol!r} has no cached bars")
    series = bars["close"]
    if years > 0:
        series = series.iloc[-(years * 252):]
    return series


class BetaPoint(BaseModel):
    date: str
    beta: float | None


class RiskResponse(BaseModel):
    symbol: str
    benchmark: str
    window: int
    years: int
    n_obs: int
    beta_series: list[BetaPoint]
    alpha_annualized: float | None
    alpha_note: str
    es_975: float | None
    ann_vol: float | None
    as_of: str | None


class MonteCarloRequest(BaseModel):
    symbol: str
    # Bounds are the resource-exhaustion guard, matching the models/simulate ceiling.
    horizon: int = Field(252, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = None


class Histogram(BaseModel):
    bin_edges: list[float]
    counts: list[int]


class MonteCarloResponse(BaseModel):
    symbol: str
    horizon: int
    n_paths: int
    histogram: Histogram
    p5: float | None
    p50: float | None
    p95: float | None
    es_975: float | None
    # Paths dropped because their terminal return overflowed to non-finite
    # (e.g. a zero/degenerate close in cached bars produces an inf daily
    # return that compounds through the block bootstrap). Stats/histogram
    # cover only the finite paths; the response is honest about the rest.
    n_nonfinite: int


@router.get("/risk/{symbol}", response_model=RiskResponse)
def risk(
    request: Request,
    symbol: str,
    window: int = Query(60, ge=5, le=756),
    years: int = Query(5, ge=1, le=25),
):
    benchmark = request.app.state.benchmark
    asset_prices = _price_series(request, symbol, years)
    bench_prices = _price_series(request, benchmark, years)

    prices = pd.concat({"asset": asset_prices, "bench": bench_prices}, axis=1).dropna()
    if len(prices) < window + 2:
        raise HTTPException(
            422, detail=f"only {len(prices)} overlapping observations; need > window+1 ({window + 1})"
        )

    asset_returns = simple_returns(prices["asset"])
    bench_returns = simple_returns(prices["bench"])

    try:
        beta = rolling_beta(asset_returns, bench_returns, window=window, rf=0.0)
        alpha = rolling_alpha(asset_returns, bench_returns, window=window, rf=0.0)
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    beta_valid = beta.dropna()
    points = [BetaPoint(date=iso(d), beta=clean(v)) for d, v in beta_valid.items()]
    points = downsample(points, _MAX_BETA_POINTS)

    alpha_valid = alpha.dropna()
    alpha_last = clean(alpha_valid.iloc[-1]) if len(alpha_valid) else None

    try:
        es = historical_es(asset_returns, confidence=0.975)
    except InsufficientDataError:
        es = None

    try:
        ann_vol = clean(annualized_vol(asset_returns))
    except InsufficientDataError:
        ann_vol = None

    return RiskResponse(
        symbol=symbol,
        benchmark=benchmark,
        window=window,
        years=years,
        n_obs=len(asset_returns),
        beta_series=points,
        alpha_annualized=alpha_last,
        alpha_note=f"vs {benchmark}, rf=0 until FRED wiring",
        es_975=clean(es),
        ann_vol=ann_vol,
        as_of=iso(prices.index[-1]) if len(prices) else None,
    )


@router.post("/risk/montecarlo", response_model=MonteCarloResponse)
def montecarlo(request: Request, req: MonteCarloRequest):
    # Full cached history (years=0) — MC needs the deepest empirical block pool available.
    prices = _price_series(request, req.symbol, years=0)
    returns = simple_returns(prices)
    if len(returns) < 30:
        raise HTTPException(422, detail=f"only {len(returns)} return observations; need >= 30")

    returns_df = returns.to_frame(name=req.symbol)
    terminal = simulate_terminal_returns(
        returns_df,
        weights=np.array([1.0]),
        n_paths=req.n_paths,
        horizon=req.horizon,
        seed=req.seed,
    )

    # A degenerate close (e.g. a zero-price bad tick) in cached bars produces
    # an inf/nan daily return that compounds through the block bootstrap into
    # non-finite terminal draws. np.histogram raises on a non-finite range —
    # guard here the same way lab.py's /api/lab/apply does: drop non-finite
    # paths, report how many, and 422 if nothing finite remains.
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

    try:
        es = historical_es(pd.Series(finite_terminal), confidence=0.975)
    except InsufficientDataError:
        es = None

    return MonteCarloResponse(
        symbol=req.symbol,
        horizon=req.horizon,
        n_paths=req.n_paths,
        histogram=Histogram(bin_edges=[float(e) for e in edges], counts=[int(c) for c in counts]),
        p5=clean(p5),
        p50=clean(p50),
        p95=clean(p95),
        es_975=clean(es),
        n_nonfinite=n_nonfinite,
    )
