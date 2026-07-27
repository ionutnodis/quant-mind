"""Risk domain routes: thin wrappers over `quantmind.risk` (Global Constraints).

GET /risk/{symbol}: rolling beta/alpha vs `app.state.benchmark`, historical ES
97.5%, annualized vol — read straight from cached bars, never network.
POST /risk/montecarlo: block-bootstrap terminal-return distribution for a
single symbol via `quantmind.risk.montecarlo`.
GET /risk/{symbol}/regression: the decomposition workbench — single- and
multi-factor OLS (Newey-West HAC SEs/CIs) via `quantmind.risk.factors`, plus
R^2 progression as factors are added and an exact variance/return
decomposition into per-factor systematic shares vs idiosyncratic. All
statistics come from `quantmind.risk.factors` (pure, golden-tested); this
router only resolves factor names to return series and serializes.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbol or
insufficient history -> structured 422, never a 500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from quantmind.api.routers._shared import clean, downsample, iso
from quantmind.risk.factors import bp_change_series, factor_regression, r_squared_progression
from quantmind.risk.montecarlo import simulate_terminal_returns
from quantmind.risk.returns import (
    InsufficientDataError,
    annualized_vol,
    historical_es,
    rolling_alpha,
    rolling_beta,
    simple_returns,
    volatility_drag,
)

router = APIRouter()

_MAX_BETA_POINTS = 500
_MAX_HIST_BINS = 60
_MAX_SCATTER_POINTS = 500
_MAX_RESIDUAL_POINTS = 500

# Named series that are decimal RATE LEVELS (FRED yields, cached like
# 0.045 = 4.5% — see quantmind.sources.fred.FRED_STORE_SERIES) rather than
# prices: a factor built from one of these is its basis-point CHANGE
# (quantmind.risk.factors.bp_change_series), not a percent return — a level
# near zero makes pct_change degenerate/explosive. Any other named series
# (e.g. NET_LIQUIDITY) falls back to simple_returns, same as a price series.
_RATE_LEVEL_SERIES = {"US10Y", "US2Y", "US3M"}


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


def _resolve_factor_series(request: Request, name: str, years: int) -> pd.Series:
    """A factor's return series: a cached symbol resolves to its simple
    price return; a named series (US10Y etc., see `_RATE_LEVEL_SERIES`
    above) resolves to its own transform. Unknown name -> structured 422
    naming both the symbol map and the named-series catalog, never a 500."""
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    if name in symbol_map:
        return simple_returns(_price_series(request, name, years))
    try:
        series = store.read_series(name)
    except FileNotFoundError:
        known = sorted(symbol_map) + store.list_series()
        raise HTTPException(422, detail=f"factor {name!r} not in cache; known: {known}")
    if years > 0:
        series = series.iloc[-(years * 252):]
    if name in _RATE_LEVEL_SERIES:
        return bp_change_series(series)
    return simple_returns(series)


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
    mean_arith_annual: float | None
    cagr: float | None
    drag_exact: float | None
    drag_approx: float | None
    drag_note: str
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

    try:
        drag = volatility_drag(asset_returns)
        mean_arith_annual = clean(drag.mean_arith_annual)
        cagr = clean(drag.cagr)
        drag_exact = clean(drag.drag_exact)
        drag_approx = clean(drag.drag_approx)
    except InsufficientDataError:
        mean_arith_annual = cagr = drag_exact = drag_approx = None

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
        mean_arith_annual=mean_arith_annual,
        cagr=cagr,
        drag_exact=drag_exact,
        drag_approx=drag_approx,
        drag_note="equity sleeve, per-symbol; drag = mean - CAGR ~= 1/2 sigma^2",
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


# --- /risk/{symbol}/regression: the decomposition workbench ---------------

_MIN_FACTOR_WINDOW = 20
_MAX_FACTOR_WINDOW = 2520


class ScatterPoint(BaseModel):
    date: str
    asset: float | None
    factor: float | None


class ResidualPoint(BaseModel):
    date: str
    value: float | None


class FitLine(BaseModel):
    """The single-factor (`factors[0]` alone) OLS line the scatter is drawn
    against. When more than one factor is requested this is DELIBERATELY not
    the same as that factor's `betas[]` entry below: `betas[]` is the
    multi-factor PARTIAL beta (holding the other factors fixed), while this
    is the simple two-variable slope — the gap between them is itself
    informative (it's what the other factors were absorbing)."""

    factor: str
    slope: float | None
    slope_se: float | None
    slope_ci: tuple[float | None, float | None]
    intercept: float | None
    r_squared: float | None


class BetaEstimate(BaseModel):
    factor: str
    beta: float | None
    se: float | None
    ci_low: float | None
    ci_high: float | None


class ShareRow(BaseModel):
    # `name` is a factor name or the literal "idiosyncratic".
    name: str
    share: float | None


class AttributionRow(BaseModel):
    # `name` is a factor name, or the literal "alpha" / "idiosyncratic".
    name: str
    daily: float | None
    annualized: float | None


class R2Step(BaseModel):
    factor_added: str
    r_squared: float | None


class RegressionResponse(BaseModel):
    symbol: str
    factors: list[str]
    window: int | None
    years: int
    n_obs: int
    hac_lags: int
    scatter: list[ScatterPoint]
    fit_line: FitLine
    alpha_daily: float | None
    alpha_annualized: float | None
    alpha_se: float | None
    alpha_ci: tuple[float | None, float | None]
    # HAC t-stat of the intercept and the annualized appraisal ratio (Jensen
    # alpha / annualized residual vol) — the "is this alpha real / worth it"
    # pair that a raw annualized-alpha number alone can't answer.
    alpha_tstat: float | None
    information_ratio: float | None
    # Honest provenance of the intercept: excess-return Jensen alpha (rf wired)
    # vs a raw-return fallback with rf=0 (see the endpoint for when each holds).
    alpha_note: str
    betas: list[BetaEstimate]
    r_squared: float | None
    r_squared_progression: list[R2Step]
    variance_decomposition: list[ShareRow]
    attribution: list[AttributionRow]
    residuals: list[ResidualPoint]
    as_of: str | None
    horizon_note: str


_PERIODS_PER_YEAR = 252


@router.get("/risk/{symbol}/regression", response_model=RegressionResponse)
def regression(
    request: Request,
    symbol: str,
    factors: str = Query(..., description="comma-separated factor names, e.g. SPY,MTUM,US10Y"),
    window: int | None = Query(None, ge=_MIN_FACTOR_WINDOW, le=_MAX_FACTOR_WINDOW),
    years: int = Query(5, ge=1, le=25),
):
    factor_names = [f.strip().upper() for f in factors.split(",") if f.strip()]
    if not factor_names:
        raise HTTPException(422, detail="factors must name at least one factor")
    if len(factor_names) != len(set(factor_names)):
        raise HTTPException(422, detail=f"duplicate factor names: {factors!r}")

    asset_returns = simple_returns(_price_series(request, symbol, years))
    factor_series = {name: _resolve_factor_series(request, name, years) for name in factor_names}

    aligned = pd.concat({"asset": asset_returns, **factor_series}, axis=1).dropna()
    if window is not None:
        aligned = aligned.tail(window)

    y = aligned["asset"]
    xs = {name: aligned[name] for name in factor_names}

    # True excess-return Jensen alpha: subtract the daily risk-free (US3M level
    # / 252) from both the asset and the ONE market factor, but only when the
    # benchmark is actually among the requested factors — otherwise there is no
    # market column to de-risk and subtracting rf from an arbitrary factor set
    # would be dishonest, so we fall back to the raw-return (rf=0) intercept.
    # A missing US3M cache also falls back to rf=0 (structured, never a 500).
    benchmark = request.app.state.benchmark
    rf_series: pd.Series | None = None
    market_factor: str | None = None
    rf_applied = False
    if benchmark in factor_names:
        try:
            rf_levels = request.app.state.store.read_series("US3M")
        except FileNotFoundError:
            rf_levels = None
        if rf_levels is not None:
            rf_series = (rf_levels / _PERIODS_PER_YEAR).reindex(aligned.index)
            market_factor = benchmark
            rf_applied = True

    try:
        full = factor_regression(y, xs, rf=rf_series, market_factor=market_factor)
        single = factor_regression(y, {factor_names[0]: xs[factor_names[0]]})
        progression = r_squared_progression(y, [(name, xs[name]) for name in factor_names])
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    if rf_applied:
        alpha_note = f"excess-return Jensen alpha vs {benchmark}, rf=US3M/252"
    elif benchmark in factor_names:
        alpha_note = f"vs {benchmark}, rf=0 (US3M unavailable)"
    else:
        alpha_note = f"rf=0; raw-return intercept ({benchmark} not among factors)"

    primary = factor_names[0]
    scatter_points = [
        ScatterPoint(date=iso(d), asset=clean(row["asset"]), factor=clean(row[primary]))
        for d, row in aligned.iterrows()
    ]
    scatter_points = downsample(scatter_points, _MAX_SCATTER_POINTS)

    fit_line = FitLine(
        factor=primary,
        slope=clean(single.betas[primary]),
        slope_se=clean(single.beta_se[primary]),
        slope_ci=(clean(single.beta_ci[primary][0]), clean(single.beta_ci[primary][1])),
        intercept=clean(single.alpha),
        r_squared=clean(single.r_squared),
    )

    betas = [
        BetaEstimate(
            factor=name,
            beta=clean(full.betas[name]),
            se=clean(full.beta_se[name]),
            ci_low=clean(full.beta_ci[name][0]),
            ci_high=clean(full.beta_ci[name][1]),
        )
        for name in factor_names
    ]

    variance_rows = [ShareRow(name=name, share=clean(full.variance_shares[name])) for name in factor_names]
    variance_rows.append(ShareRow(name="idiosyncratic", share=clean(full.variance_shares["idiosyncratic"])))

    def _attribution_row(name: str, daily: float | None) -> AttributionRow:
        annualized = daily * _PERIODS_PER_YEAR if daily is not None else None
        return AttributionRow(name=name, daily=clean(daily), annualized=clean(annualized))

    attribution_rows = [_attribution_row("alpha", full.attribution["alpha"])]
    attribution_rows += [_attribution_row(name, full.attribution[name]) for name in factor_names]
    attribution_rows.append(_attribution_row("idiosyncratic", full.attribution["idiosyncratic"]))

    residual_points = downsample(
        [ResidualPoint(date=iso(d), value=clean(v)) for d, v in full.residuals.items()], _MAX_RESIDUAL_POINTS
    )

    alpha_daily = full.alpha
    return RegressionResponse(
        symbol=symbol,
        factors=factor_names,
        window=window,
        years=years,
        n_obs=full.n_obs,
        hac_lags=full.hac_lags,
        scatter=scatter_points,
        fit_line=fit_line,
        alpha_daily=clean(alpha_daily),
        alpha_annualized=clean(alpha_daily * _PERIODS_PER_YEAR),
        alpha_se=clean(full.alpha_se),
        alpha_ci=(clean(full.alpha_ci[0]), clean(full.alpha_ci[1])),
        alpha_tstat=clean(full.alpha_tstat),
        information_ratio=clean(full.information_ratio),
        alpha_note=alpha_note,
        betas=betas,
        r_squared=clean(full.r_squared),
        r_squared_progression=[R2Step(factor_added=name, r_squared=clean(r)) for name, r in progression],
        variance_decomposition=variance_rows,
        attribution=attribution_rows,
        residuals=residual_points,
        as_of=iso(aligned.index[-1]) if len(aligned) else None,
        horizon_note=(
            "daily returns; alpha/attribution figures shown daily and annualized (x252); "
            f"n_obs is {'the full ' + str(years) + 'y cache' if window is None else f'the last {window} aligned observations'}"
        ),
    )
