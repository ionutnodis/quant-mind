"""lab domain routes — the model-registry apply-to-book pipeline (Task 3, the
Lab bench centerpiece per DESIGN.md), plus the wave-3B practitioner routes.

POST /api/lab/apply deliberately does NOT duplicate /api/models/{name}/fit or
/simulate (quantmind/api/app.py) — the frontend calls those directly for
fit/simulate, and this endpoint reuses the same FitResult -> model.simulate()
path, then pipes the terminal factor draws through
quantmind.exposure.bridge.apply_to_book into a P&L distribution.
UnsupportedMappingError (wrong exposure units/kind) becomes a 422 with the
bridge's own "refusing" message — never a dimensionally wrong number.
Simulation start defaults to the fitted series' last observation (the
`x_last` diagnostic every OU fit now carries), overridable via x0.

POST /api/lab/book-regression (wave-3B spec item 1): regress the book's daily
$P&L on the daily basis-point change of a cached FRED-named rate series
(default US10Y, stored in DECIMALS — bp_change_series does the ×1e4
conversion) with Newey-West HAC SEs. The book comes inline or via book_ref
(Task A1's spine); returns are the same |MV|-signed-weight construction
whatif/hedge use, scaled by the book's gross into dollars, so the estimated
beta is directly the `usd_per_bp` exposure Apply-to-Book consumes — the
one-click "tie the model back to the book" hand-off.

POST /api/lab/pair (wave-3B spec item 5 — hedge-pair discovery lives HERE
now, removed from the Hedge Lab in wave-3A by design): Engle-Granger on two
cached instruments -> OU fit on the spread y - β·x -> z-score bands payload
(μ, stationary σ, current displacement) + the full OU fit for transparency.
A non-cointegrated pair is an honest 200 with is_cointegrated=false, and a
spread that fails the random-walk gate says so — never a fake band chart.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.analytics.cointegration import engle_granger
from quantmind.api.app import FitResponse
from quantmind.api.routers._shared import (
    PositionIn,
    clean,
    downsample,
    iso,
    read_close_series,
    weighted_portfolio_returns,
)
from quantmind.api.routers.book import read_book_positions
from quantmind.exposure.bridge import Exposure, UnsupportedMappingError, apply_to_book
from quantmind.models.base import FitResult
from quantmind.models.registry import get_model
from quantmind.risk.factors import bp_change_series, factor_regression
from quantmind.risk.returns import InsufficientDataError

router = APIRouter()

_MAX_BOOK_POSITIONS = 50
_MIN_PAIR_OBS = 30
_MAX_SPREAD_POINTS = 500


class ExposureRequest(BaseModel):
    factor_kind: str
    units: str
    value: float


class LabApplyRequest(BaseModel):
    model_name: str
    fit: FitResponse
    # Same resource-exhaustion guard as /api/models/{name}/simulate.
    horizon: int = Field(126, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = None
    x0: float | None = None
    exposure: ExposureRequest


class PnlHistogram(BaseModel):
    # Named distinctly from risk.py's Histogram (same shape, different field
    # name until this unification) so FastAPI's OpenAPI schema doesn't mangle
    # two same-named-but-different models into Histogram/Histogram1.
    bin_edges: list[float]
    counts: list[int]


class LabApplyResponse(BaseModel):
    histogram: PnlHistogram
    mean: float | None
    p5: float | None
    p50: float | None
    p95: float | None
    es: float | None
    horizon: int
    n_paths: int
    # Paths dropped because their P&L overflowed to non-finite (explosive
    # fits). Stats/histogram cover only the finite paths; the UI is honest
    # about how many were excluded.
    n_nonfinite: int


def _tail_es(pnl: np.ndarray, confidence: float = 0.975) -> float | None:
    """Mean of the worst floor(n*(1-confidence)) P&L draws (a loss, reported as-is)."""
    n_tail = math.floor(len(pnl) * (1.0 - confidence) + 1e-9)
    if n_tail < 1:
        return None
    tail = np.sort(pnl)[:n_tail]
    return float(np.mean(tail))


@router.post("/lab/apply", response_model=LabApplyResponse)
def apply_to_book_route(req: LabApplyRequest) -> LabApplyResponse:
    try:
        model = get_model(req.model_name)
    except KeyError as e:
        raise HTTPException(404, detail=str(e))

    fit_result = FitResult(
        model_name=req.fit.model_name,
        params=req.fit.params,
        cis=req.fit.cis,
        diagnostics=req.fit.diagnostics,
        n_obs=req.fit.n_obs,
    )
    # Start where reality is (wave-3B spec item 3): explicit x0 wins, then the
    # fitted series' last observation, then the long-run mean as last resort.
    initial = req.x0
    if initial is None:
        initial = req.fit.diagnostics.get("x_last")
    if initial is None:
        initial = req.fit.params.get("mu")
    if initial is None:
        raise HTTPException(422, detail="fit has no 'x_last'/'mu'; provide x0 explicitly")

    paths = model.simulate(
        fit_result, horizon=req.horizon, n_paths=req.n_paths, seed=req.seed, x0=initial
    )
    exposure = Exposure(
        factor_kind=req.exposure.factor_kind,
        units=req.exposure.units,
        value=req.exposure.value,
    )
    try:
        pnl = apply_to_book(paths, initial=initial, factor=model.factor, exposure=exposure)
    except UnsupportedMappingError as e:
        raise HTTPException(422, detail=str(e))

    # Explosive fits (e.g. theta estimated negative on a non-stationary
    # window) can overflow paths/pnl to inf/nan. np.histogram raises on a
    # non-finite range — guard here so the endpoint never 500s: drop
    # non-finite paths, report how many, and 422 if nothing finite remains.
    finite_pnl = pnl[np.isfinite(pnl)]
    n_nonfinite = int(len(pnl) - len(finite_pnl))
    if len(finite_pnl) == 0:
        raise HTTPException(
            422,
            detail="simulation produced no finite P&L — check fit stability / diagnostics",
        )

    n_bins = min(60, max(1, len(finite_pnl)))
    counts, edges = np.histogram(finite_pnl, bins=n_bins)
    p5, p50, p95 = (float(v) for v in np.percentile(finite_pnl, [5, 50, 95]))

    return LabApplyResponse(
        histogram=PnlHistogram(
            bin_edges=[float(e) for e in edges], counts=[int(c) for c in counts]
        ),
        mean=clean(float(np.mean(finite_pnl))),
        p5=clean(p5),
        p50=clean(p50),
        p95=clean(p95),
        es=clean(_tail_es(finite_pnl)),
        horizon=req.horizon,
        n_paths=req.n_paths,
        n_nonfinite=n_nonfinite,
    )


# --- book-derived exposure regression (wave-3B spec item 1) ---


class BookRegressionRequest(BaseModel):
    # Exactly one of `book` (inline positions) or `book_ref` (a pinned
    # snapshot id, Task A1's spine) — same contract as whatif/hedge.
    book: list[PositionIn] | None = Field(None, min_length=1, max_length=_MAX_BOOK_POSITIONS)
    book_ref: str | None = None
    factor_series: str = Field("US10Y", min_length=1, max_length=64)
    years: int = Field(5, ge=1, le=25)

    @model_validator(mode="after")
    def _book_xor_book_ref(self) -> "BookRegressionRequest":
        if bool(self.book) == bool(self.book_ref):
            raise ValueError("provide exactly one of book or book_ref")
        return self


class BookRegressionResponse(BaseModel):
    factor_series: str
    # Every risk number is horizon-labeled (wave-3 Global Constraint): the
    # regression runs on DAILY differences; the beta is a daily sensitivity.
    horizon: Literal["daily"] = "daily"
    # The estimated beta IS an Apply-to-Book exposure: $ P&L per 1bp move.
    exposure_units: Literal["usd_per_bp"] = "usd_per_bp"
    beta_usd_per_bp: float | None
    beta_se: float | None
    beta_ci: tuple[float, float] | None
    alpha_usd: float | None
    alpha_se: float | None
    r_squared: float | None
    n_obs: int
    hac_lags: int
    book_gross: float | None
    as_of: str | None


def _resolve_book(store, book: list[PositionIn] | None, book_ref: str | None) -> list[PositionIn]:
    positions = book if book is not None else read_book_positions(store, book_ref)
    if not positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(positions) > _MAX_BOOK_POSITIONS:
        raise HTTPException(
            422, detail=f"book has {len(positions)} positions; max {_MAX_BOOK_POSITIONS}"
        )
    return positions


@router.post("/lab/book-regression", response_model=BookRegressionResponse)
def book_regression(request: Request, req: BookRegressionRequest) -> BookRegressionResponse:
    store = request.app.state.store
    symbol_map = store.read_symbol_map()

    positions = _resolve_book(store, req.book, req.book_ref)
    symbols = list(dict.fromkeys(p.symbol for p in positions))
    qtys: dict[str, float] = {}
    for p in positions:
        qtys[p.symbol] = qtys.get(p.symbol, 0.0) + p.qty

    unknown = sorted(s for s in symbols if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")

    series_map = {s: read_close_series(store, symbol_map[s], s, req.years) for s in symbols}
    unpriceable = sorted(s for s in symbols if clean(float(series_map[s].iloc[-1])) is None)
    if unpriceable:
        raise HTTPException(
            422,
            detail=f"non-finite last close in cached bars for: {unpriceable} — re-sync before computing",
        )

    # Same |MV|-signed-weight construction whatif/hedge use, then scaled by
    # the book's gross into DOLLAR P&L (constant-notional approximation) so
    # the regression beta lands directly in usd_per_bp.
    last_close = {s: float(series_map[s].iloc[-1]) for s in symbols}
    market_values = {s: qtys[s] * last_close[s] for s in symbols}
    gross = sum(abs(v) for v in market_values.values())
    if gross <= 0:
        raise HTTPException(422, detail="portfolio has zero gross market value")
    weights = np.array([market_values[s] / gross for s in symbols])

    prices = pd.concat({s: series_map[s] for s in symbols}, axis=1).dropna()
    returns = prices.pct_change().dropna()
    if len(returns) == 0:
        raise HTTPException(422, detail="book has no overlapping trading days")
    book_pnl = weighted_portfolio_returns(returns, symbols, weights) * gross

    try:
        levels = store.read_series(req.factor_series)
    except FileNotFoundError:
        raise HTTPException(
            422,
            detail=f"named series {req.factor_series!r} not in cache; known: {store.list_series()}",
        )
    if req.years > 0:
        levels = levels.iloc[-(req.years * 252):]
    factor_bp = bp_change_series(levels)

    factor_name = f"d_{req.factor_series.lower()}_bp"
    try:
        result = factor_regression(book_pnl, {factor_name: factor_bp})
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    return BookRegressionResponse(
        factor_series=req.factor_series,
        beta_usd_per_bp=clean(result.betas[factor_name]),
        beta_se=clean(result.beta_se[factor_name]),
        beta_ci=result.beta_ci[factor_name],
        alpha_usd=clean(result.alpha),
        alpha_se=clean(result.alpha_se),
        r_squared=clean(result.r_squared),
        n_obs=result.n_obs,
        hac_lags=result.hac_lags,
        book_gross=clean(gross),
        as_of=iso(result.residuals.index[-1]) if len(result.residuals) else None,
    )


# --- EG→OU pair pipeline (wave-3B spec item 5) ---


class PairRequest(BaseModel):
    y_symbol: str = Field(..., min_length=1, max_length=32)
    x_symbol: str = Field(..., min_length=1, max_length=32)
    years: int = Field(5, ge=1, le=25)


class PairResponse(BaseModel):
    y_symbol: str
    x_symbol: str
    horizon: Literal["daily"] = "daily"
    coint_pvalue: float | None
    hedge_ratio: float | None
    # OLS SE of the hedge ratio (uncertainty is displayed) — an approximation
    # under cointegration (superconsistent first stage), shown as scale only.
    hedge_ratio_se: float | None
    is_cointegrated: bool
    # z-bands chart payload: the spread y - β·x (downsampled), its OU long-run
    # mean and stationary σ (bands at μ±1σ/±2σ), and the current displacement.
    dates: list[str]
    spread: list[float | None]
    mu: float | None
    stationary_sigma: float | None
    current_z: float | None
    half_life_days: float | None
    half_life_ci: tuple[float, float] | None
    mean_reversion_established: bool
    # Full mathematical transparency: the raw OU fit on the spread.
    fit: FitResponse
    n_obs: int
    as_of: str | None


@router.post("/lab/pair", response_model=PairResponse)
def pair_pipeline(request: Request, req: PairRequest) -> PairResponse:
    store = request.app.state.store
    symbol_map = store.read_symbol_map()

    if req.y_symbol == req.x_symbol:
        raise HTTPException(422, detail="pick two different instruments")
    unknown = sorted(s for s in (req.y_symbol, req.x_symbol) if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")

    y = read_close_series(store, symbol_map[req.y_symbol], req.y_symbol, req.years)
    x = read_close_series(store, symbol_map[req.x_symbol], req.x_symbol, req.years)
    aligned = pd.concat({"y": y, "x": x}, axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(aligned) < _MIN_PAIR_OBS:
        raise HTTPException(
            422,
            detail=f"only {len(aligned)} overlapping observations; need >= {_MIN_PAIR_OBS}",
        )

    try:
        eg = engle_granger(aligned["y"], aligned["x"])
    except (ValueError, np.linalg.LinAlgError) as e:
        raise HTTPException(422, detail=f"Engle-Granger failed: {e}")

    spread = aligned["y"] - eg.hedge_ratio * aligned["x"]
    model = get_model("ou")
    try:
        fit_result = model.fit(spread)
    except ValueError as e:
        raise HTTPException(422, detail=f"OU fit on spread failed: {e}")

    d = fit_result.diagnostics
    half_life = d.get("half_life_days")
    half_life_ci = None
    if half_life is not None and "half_life_ci_lo" in d and "half_life_ci_hi" in d:
        half_life_ci = (d["half_life_ci_lo"], d["half_life_ci_hi"])

    spread_ds = downsample(spread, _MAX_SPREAD_POINTS)
    return PairResponse(
        y_symbol=req.y_symbol,
        x_symbol=req.x_symbol,
        coint_pvalue=clean(eg.pvalue),
        hedge_ratio=clean(eg.hedge_ratio),
        hedge_ratio_se=clean(eg.hedge_ratio_se),
        is_cointegrated=eg.is_cointegrated(),
        dates=[iso(ts) for ts in spread_ds.index],
        spread=[clean(v) for v in spread_ds.to_numpy()],
        mu=clean(fit_result.params.get("mu")),
        stationary_sigma=clean(d.get("stationary_sigma")),
        current_z=clean(d.get("displacement_sigma")),
        half_life_days=clean(half_life),
        half_life_ci=half_life_ci,
        mean_reversion_established=d.get("mean_reversion") == 1.0,
        fit=FitResponse(
            model_name=fit_result.model_name,
            params={k: clean(v) for k, v in fit_result.params.items()},
            cis=fit_result.cis,
            diagnostics={k: clean(v) for k, v in fit_result.diagnostics.items()},
            n_obs=fit_result.n_obs,
        ),
        n_obs=len(aligned),
        as_of=iso(spread.index[-1]),
    )
