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

from quantmind.api.routers._shared import (
    FxEvidenceOut,
    clean,
    complete_fx_evidence,
    downsample,
    iso,
    latest_observation_is_future,
    load_base_currency_series,
)
from quantmind.fx import FxConverter
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
_PERIODS_PER_YEAR = 252

# Named series that are decimal RATE LEVELS (FRED yields, cached like
# 0.045 = 4.5% — see quantmind.sources.fred.FRED_STORE_SERIES) rather than
# prices: a factor built from one of these is its basis-point CHANGE
# (quantmind.risk.factors.bp_change_series), not a percent return — a level
# near zero makes pct_change degenerate/explosive. Any other named series
# (e.g. NET_LIQUIDITY) falls back to simple_returns, same as a price series.
_RATE_LEVEL_SERIES = {"US10Y", "US2Y", "US3M"}


def _daily_risk_free(
    request: Request, index: pd.Index, *, min_obs: int
) -> tuple[pd.Series | None, str | None]:
    """Resolve aligned daily US3M or explain why production alpha is unavailable."""
    base_currency = getattr(request.app.state, "base_currency", "USD")
    if base_currency != "USD":
        return (
            None,
            f"{base_currency} risk-free evidence is not configured; US3M is USD-only",
        )
    try:
        levels = request.app.state.store.read_series("US3M")
    except FileNotFoundError:
        return None, "US3M risk-free series is not cached"
    try:
        if latest_observation_is_future(levels):
            return None, "US3M risk-free series is future-dated; run sync"
    except (TypeError, ValueError):
        return None, "US3M risk-free series has an invalid observation date"

    daily = (levels / _PERIODS_PER_YEAR).replace([np.inf, -np.inf], np.nan).reindex(index)
    n_obs = int(daily.notna().sum())
    if n_obs < min_obs:
        return None, f"US3M has only {n_obs} aligned observations; need at least {min_obs}"
    return daily, None


def _price_series_map(
    request: Request, symbols: list[str], years: int
) -> tuple[dict[str, pd.Series], FxConverter]:
    """Load every priced input through one fail-closed FX evidence set."""
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    ordered = list(dict.fromkeys(symbols))
    for symbol in ordered:
        if symbol not in symbol_map:
            raise HTTPException(422, detail=f"symbol {symbol!r} not in cache")
    series, _, converter = load_base_currency_series(
        store,
        symbol_map,
        ordered,
        years=years,
        base_currency=getattr(request.app.state, "base_currency", "USD"),
    )
    return series, converter


def _resolve_factor_levels(
    request: Request,
    name: str,
    years: int,
    price_series: dict[str, pd.Series],
) -> pd.Series:
    """Resolve raw factor levels before constructing a common return calendar.

    Cached symbols and named series are deliberately returned untransformed:
    the regression route first inner-joins all levels, then computes every
    return/change over the same pair of dates. Transforming independently and
    aligning afterwards creates unequal holding periods around calendar gaps.
    Unknown names remain a structured 422, never a 500.
    """
    store = request.app.state.store
    if name in price_series:
        return price_series[name]
    try:
        series = store.read_series(name)
    except FileNotFoundError:
        symbol_map = store.read_symbol_map()
        known = sorted(symbol_map) + store.list_series()
        raise HTTPException(422, detail=f"factor {name!r} not in cache; known: {known}")
    try:
        future_dated = latest_observation_is_future(series)
    except (TypeError, ValueError):
        raise HTTPException(
            422, detail=f"factor {name!r} has an invalid cached observation date"
        )
    if future_dated:
        raise HTTPException(
            422, detail=f"factor {name!r} has future-dated cached data; run sync"
        )
    if years > 0:
        series = series.iloc[-(years * 252):]
    return series


class BetaPoint(BaseModel):
    date: str
    beta: float | None


class RiskResponse(BaseModel):
    fx: FxEvidenceOut
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
    fx: FxEvidenceOut
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
    price_series, fx_converter = _price_series_map(
        request, [symbol, benchmark], years
    )
    asset_prices = price_series[symbol]
    bench_prices = price_series[benchmark]

    prices = pd.concat({"asset": asset_prices, "bench": bench_prices}, axis=1).dropna()
    if len(prices) < window + 2:
        raise HTTPException(
            422, detail=f"only {len(prices)} overlapping observations; need > window+1 ({window + 1})"
        )

    asset_returns = simple_returns(prices["asset"])
    bench_returns = simple_returns(prices["bench"])

    rf_series, rf_unavailable_reason = _daily_risk_free(
        request, asset_returns.index, min_obs=window
    )

    try:
        beta = rolling_beta(asset_returns, bench_returns, window=window, rf=rf_series)
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    beta_valid = beta.dropna()
    points = [BetaPoint(date=iso(d), beta=clean(v)) for d, v in beta_valid.items()]
    points = downsample(points, _MAX_BETA_POINTS)

    alpha_last = None
    if rf_series is not None:
        alpha = rolling_alpha(asset_returns, bench_returns, window=window, rf=rf_series)
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
        fx=complete_fx_evidence(
            fx_converter, base_currency=request.app.state.base_currency
        ),
        symbol=symbol,
        benchmark=benchmark,
        window=window,
        years=years,
        n_obs=len(asset_returns),
        beta_series=points,
        alpha_annualized=alpha_last,
        alpha_note=(
            f"excess-return Jensen alpha vs {benchmark}, rf=US3M/252"
            if rf_series is not None
            else f"alpha unavailable: {rf_unavailable_reason}"
        ),
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
    price_series, fx_converter = _price_series_map(request, [req.symbol], years=0)
    prices = price_series[req.symbol]
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
        fx=complete_fx_evidence(
            fx_converter, base_currency=request.app.state.base_currency
        ),
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
    fx: FxEvidenceOut
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
    # Honest provenance of the intercept: excess-return Jensen alpha when the
    # market factor and aligned risk-free series are available; otherwise all
    # alpha fields are suppressed.
    alpha_note: str
    betas: list[BetaEstimate]
    r_squared: float | None
    r_squared_progression: list[R2Step]
    variance_decomposition: list[ShareRow]
    attribution: list[AttributionRow]
    residuals: list[ResidualPoint]
    as_of: str | None
    horizon_note: str


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

    symbol_map = request.app.state.store.read_symbol_map()
    priced_factors = [name for name in factor_names if name in symbol_map]
    price_series, fx_converter = _price_series_map(
        request, [symbol, *priced_factors], years
    )
    factor_levels = {
        name: _resolve_factor_levels(request, name, years, price_series)
        for name in factor_names
    }
    common_levels = pd.concat(
        {"asset": price_series[symbol], **factor_levels},
        axis=1,
        sort=False,
    ).dropna()
    aligned = pd.DataFrame(index=common_levels.index[1:])
    aligned["asset"] = simple_returns(common_levels["asset"])
    for name in factor_names:
        aligned[name] = (
            bp_change_series(common_levels[name])
            if name in _RATE_LEVEL_SERIES
            else simple_returns(common_levels[name])
        )
    aligned = aligned.dropna()
    if window is not None:
        aligned = aligned.tail(window)

    y = aligned["asset"]
    xs = {name: aligned[name] for name in factor_names}

    # True excess-return Jensen alpha requires both the market factor and an
    # aligned daily risk-free series. The raw OLS fit remains useful for beta,
    # variance, and residual diagnostics when either input is missing, but its
    # intercept must never be published as production alpha.
    benchmark = request.app.state.benchmark
    rf_series: pd.Series | None = None
    market_factor: str | None = None
    alpha_unavailable_reason: str | None = None
    if benchmark in factor_names:
        rf_series, alpha_unavailable_reason = _daily_risk_free(
            request,
            aligned.index,
            min_obs=max(_MIN_FACTOR_WINDOW, 5 * (len(factor_names) + 1)),
        )
        if rf_series is not None:
            market_factor = benchmark
    else:
        alpha_unavailable_reason = f"benchmark {benchmark} is not among factors"

    alpha_available = rf_series is not None and market_factor is not None

    try:
        full = factor_regression(y, xs, rf=rf_series, market_factor=market_factor)
        primary = factor_names[0]
        single = factor_regression(
            y,
            {primary: xs[primary]},
            rf=rf_series if alpha_available and primary == benchmark else None,
            market_factor=benchmark if alpha_available and primary == benchmark else None,
        )
        progression = r_squared_progression(y, [(name, xs[name]) for name in factor_names])
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    if alpha_available:
        alpha_note = f"excess-return Jensen alpha vs {benchmark}, rf=US3M/252"
    else:
        alpha_note = f"alpha unavailable: {alpha_unavailable_reason}"

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
        intercept=clean(single.alpha) if alpha_available and primary == benchmark else None,
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

    attribution_rows = [
        _attribution_row("alpha", full.attribution["alpha"] if alpha_available else None)
    ]
    attribution_rows += [_attribution_row(name, full.attribution[name]) for name in factor_names]
    attribution_rows.append(_attribution_row("idiosyncratic", full.attribution["idiosyncratic"]))

    residual_points = downsample(
        [ResidualPoint(date=iso(d), value=clean(v)) for d, v in full.residuals.items()], _MAX_RESIDUAL_POINTS
    )

    alpha_daily = full.alpha if alpha_available else None
    return RegressionResponse(
        fx=complete_fx_evidence(
            fx_converter, base_currency=request.app.state.base_currency
        ),
        symbol=symbol,
        factors=factor_names,
        window=window,
        years=years,
        n_obs=full.n_obs,
        hac_lags=full.hac_lags,
        scatter=scatter_points,
        fit_line=fit_line,
        alpha_daily=clean(alpha_daily),
        alpha_annualized=clean(
            alpha_daily * _PERIODS_PER_YEAR if alpha_daily is not None else None
        ),
        alpha_se=clean(full.alpha_se) if alpha_available else None,
        alpha_ci=(
            (clean(full.alpha_ci[0]), clean(full.alpha_ci[1]))
            if alpha_available
            else (None, None)
        ),
        alpha_tstat=clean(full.alpha_tstat) if alpha_available else None,
        information_ratio=clean(full.information_ratio) if alpha_available else None,
        alpha_note=alpha_note,
        betas=betas,
        r_squared=clean(full.r_squared),
        r_squared_progression=[R2Step(factor_added=name, r_squared=clean(r)) for name, r in progression],
        variance_decomposition=variance_rows,
        attribution=attribution_rows,
        residuals=residual_points,
        as_of=iso(aligned.index[-1]) if len(aligned) else None,
        horizon_note=(
            "daily returns; Jensen alpha is shown only with aligned US3M and the market factor; "
            "available attribution figures are shown daily and annualized (x252); "
            f"n_obs is {'the full ' + str(years) + 'y cache' if window is None else f'the last {window} aligned observations'}"
        ),
    )
