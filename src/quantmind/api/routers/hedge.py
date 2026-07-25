"""hedge domain routes — the Hedge Lab (DESIGN.md IA #4): "decisions, not
analytics." POST /api/hedge takes a book + a beta_target objective and
returns candidates ranked by protection (ES reduction), sized to move the
book's beta to target. Cointegration p-value is a DIAGNOSTIC column only
(Engineering Constraint 12) — it never drives the ranking.

Thin composition over the tested pure core only (Global Constraints):
quantmind.risk.returns for beta/ES, quantmind.analytics.correlation for the
rolling correlation-stability diagnostic, quantmind.analytics.cointegration
for the Engle-Granger diagnostic. No math beyond wiring lives here.

Alignment approach mirrors routers/whatif.py: price-level inner join across
every symbol involved, then pct_change, weights by |market value|-signed.

Hedge sizing: to move book beta from `book_beta` to `objective.value` by
adding `hedge_qty` shares of a candidate with beta `beta_cand` at price
`price_cand`, the dollar-beta needed from the hedge leg is
`(objective.value - book_beta) * book_value`, so
`hedge_qty = (objective.value - book_beta) * book_value / (beta_cand * price_cand)`
`= -(book_beta - objective.value) * book_value / (beta_cand * price_cand)`.
A candidate with |beta| < 0.1 is flagged `unusable` (sizing would blow up)
and reported without a size/protection, never dropped from the response.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or an empty candidate universe -> structured 422, never a 500 (pattern:
routers/risk.py, routers/whatif.py).
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from quantmind.analytics.cointegration import engle_granger
from quantmind.analytics.correlation import rolling_correlation
from quantmind.risk.returns import InsufficientDataError, historical_es, rolling_beta

router = APIRouter()

_BETA_WINDOW = 60
_MIN_BETA_ABS = 0.1
_MAX_CANDIDATES_OUT = 20
_MIN_COINT_OBS = 10


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


class BookPositionIn(BaseModel):
    symbol: str = Field(..., min_length=1)
    qty: float

    @field_validator("qty")
    @classmethod
    def _qty_nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("qty must be nonzero")
        return v


class Objective(BaseModel):
    kind: Literal["beta_target"] = "beta_target"
    value: float = Field(..., ge=-2.0, le=2.0)


class HedgeRequest(BaseModel):
    book: list[BookPositionIn] = Field(..., min_length=1, max_length=50)
    objective: Objective
    # Default = the cached universe minus book symbols (resolved in-handler,
    # request.app.state.store isn't available at model-validation time).
    candidates: list[str] | None = None
    years: int = Field(5, ge=1, le=25)


class HedgeCandidateOut(BaseModel):
    symbol: str
    beta: float | None
    unusable: bool
    hedge_qty: float | None
    hedge_notional: float | None
    es_before: float | None
    es_after: float | None
    protection: float | None
    residual_beta: float | None
    # Diagnostic only (Engineering Constraint 12) — never the ranking key.
    corr_stability: float | None
    coint_pvalue: float | None


class HedgeResponse(BaseModel):
    benchmark: str
    objective: Objective
    book_value: float | None
    book_beta: float | None
    es_before: float | None
    n_candidates_evaluated: int
    candidates: list[HedgeCandidateOut]
    as_of: str | None


def _read_close_series(store, con_id: int, symbol: str, years: int) -> pd.Series:
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


def _portfolio_returns(
    series_map: dict[str, pd.Series], qtys: dict[str, float], symbols: list[str]
) -> tuple[pd.Series | None, dict[str, float], float, pd.DataFrame]:
    """Price-level inner join across `symbols`, then pct_change, weighted by
    |market value|-signed weight (mirrors routers/whatif.py's alignment)."""
    last_close = {s: float(series_map[s].iloc[-1]) for s in symbols}
    market_values = {s: qtys[s] * last_close[s] for s in symbols}
    gross = sum(abs(v) for v in market_values.values())
    weights = {s: (market_values[s] / gross if gross else 0.0) for s in symbols}
    book_value = sum(market_values.values())

    prices = pd.concat({s: series_map[s] for s in symbols}, axis=1).dropna()
    returns = prices.pct_change().dropna()
    if len(returns) == 0:
        return None, weights, book_value, prices
    weights_arr = np.array([weights[s] for s in symbols])
    portfolio_returns = pd.Series(returns[symbols].to_numpy() @ weights_arr, index=returns.index)
    return portfolio_returns, weights, book_value, prices


@router.post("/hedge", response_model=HedgeResponse)
def hedge(request: Request, req: HedgeRequest) -> HedgeResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    symbol_map = store.read_symbol_map()

    unique_book = list(dict.fromkeys(p.symbol for p in req.book))
    qtys: dict[str, float] = {}
    for p in req.book:
        qtys[p.symbol] = qtys.get(p.symbol, 0.0) + p.qty

    unknown = sorted(s for s in unique_book if s not in symbol_map)
    if req.candidates is not None:
        unknown += sorted(s for s in req.candidates if s not in symbol_map and s not in unknown)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")
    if benchmark not in symbol_map:
        raise HTTPException(422, detail=f"benchmark {benchmark!r} not in cache")

    series_map: dict[str, pd.Series] = {}
    for sym in [*unique_book, benchmark]:
        series_map[sym] = _read_close_series(store, symbol_map[sym], sym, req.years)

    book_returns, _weights, book_value, book_prices = _portfolio_returns(series_map, qtys, unique_book)
    if book_returns is None:
        raise HTTPException(422, detail="book has no overlapping trading days")

    bench_returns = series_map[benchmark].pct_change().dropna()
    aligned = pd.concat({"book": book_returns, "bench": bench_returns}, axis=1).dropna()
    if len(aligned) < _BETA_WINDOW + 2:
        raise HTTPException(
            422,
            detail=(
                f"only {len(aligned)} overlapping book/benchmark observations; "
                f"need > window+1 ({_BETA_WINDOW + 1})"
            ),
        )

    try:
        beta_series = rolling_beta(aligned["book"], aligned["bench"], window=_BETA_WINDOW, rf=0.0)
        beta_valid = beta_series.dropna()
        book_beta = _clean(float(beta_valid.iloc[-1])) if len(beta_valid) else None
    except InsufficientDataError:
        book_beta = None

    try:
        es_before = _clean(historical_es(book_returns, confidence=0.975))
    except InsufficientDataError:
        es_before = None

    # Book-value time series (price level, diagnostic only): sum(qty_i *
    # price_i(t)) over the book's own inner-joined price index — the y series
    # for the Engle-Granger cointegration diagnostic below.
    book_value_series = sum(book_prices[s] * qtys[s] for s in unique_book)

    if req.candidates is not None:
        candidate_pool = [s for s in dict.fromkeys(req.candidates) if s not in unique_book]
    else:
        candidate_pool = [s for s in symbol_map if s not in unique_book]

    if not candidate_pool:
        raise HTTPException(
            422,
            detail="no usable candidates: candidate universe is empty after excluding book symbols",
        )

    results: list[HedgeCandidateOut] = []
    for csym in candidate_pool:
        try:
            cand_prices = _read_close_series(store, symbol_map[csym], csym, req.years)
        except HTTPException:
            continue  # mapped but no cached bars — a data gap, not a client error; skip it

        cand_returns = cand_prices.pct_change().dropna()

        aligned_c = pd.concat({"asset": cand_returns, "bench": bench_returns}, axis=1).dropna()
        beta_cand: float | None = None
        if len(aligned_c) >= _BETA_WINDOW + 2:
            try:
                cb_series = rolling_beta(aligned_c["asset"], aligned_c["bench"], window=_BETA_WINDOW, rf=0.0)
                cb_valid = cb_series.dropna()
                if len(cb_valid):
                    beta_cand = float(cb_valid.iloc[-1])
            except InsufficientDataError:
                beta_cand = None

        aligned_bc = pd.concat({"book": book_returns, "cand": cand_returns}, axis=1).dropna()
        corr_stability: float | None = None
        if len(aligned_bc) >= _BETA_WINDOW + 2:
            roll_corr = rolling_correlation(aligned_bc["book"], aligned_bc["cand"], window=_BETA_WINDOW).dropna()
            if len(roll_corr):
                corr_stability = _clean(float(roll_corr.std()))

        coint_pvalue: float | None = None
        aligned_coint = pd.concat({"y": book_value_series, "x": cand_prices}, axis=1).dropna()
        if len(aligned_coint) >= _MIN_COINT_OBS:
            try:
                coint_pvalue = _clean(float(engle_granger(aligned_coint["y"], aligned_coint["x"]).pvalue))
            except Exception:
                # Diagnostic-only column (Engineering Constraint 12): a
                # degenerate candidate series (e.g. zero-variance price) can
                # make the underlying OLS/ADF machinery raise (singular
                # design matrix, etc). Never let a diagnostic failure 500 the
                # whole ranking — just omit it for this candidate.
                coint_pvalue = None

        unusable = beta_cand is None or not math.isfinite(beta_cand) or abs(beta_cand) < _MIN_BETA_ABS

        hedge_qty = hedge_notional = es_after = protection = residual_beta = None

        if not unusable and book_beta is not None:
            price_cand_last = float(cand_prices.iloc[-1])
            if math.isfinite(price_cand_last) and price_cand_last != 0:
                raw_size = (book_beta - req.objective.value) * book_value / (beta_cand * price_cand_last)
                hedge_qty = -raw_size
                hedge_notional = hedge_qty * price_cand_last

                hedge_symbols = [*unique_book, csym]
                hedge_qtys = dict(qtys)
                hedge_qtys[csym] = hedge_qtys.get(csym, 0.0) + hedge_qty
                hedge_series_map = dict(series_map)
                hedge_series_map[csym] = cand_prices

                hedged_returns, _hw, _hv, _hp = _portfolio_returns(hedge_series_map, hedge_qtys, hedge_symbols)
                if hedged_returns is not None:
                    try:
                        es_after = _clean(historical_es(hedged_returns, confidence=0.975))
                    except InsufficientDataError:
                        es_after = None
                    if es_before is not None and es_after is not None:
                        protection = es_before - es_after

                    aligned_h = pd.concat({"book": hedged_returns, "bench": bench_returns}, axis=1).dropna()
                    if len(aligned_h) >= _BETA_WINDOW + 2:
                        try:
                            rb_series = rolling_beta(
                                aligned_h["book"], aligned_h["bench"], window=_BETA_WINDOW, rf=0.0
                            )
                            rb_valid = rb_series.dropna()
                            if len(rb_valid):
                                residual_beta = _clean(float(rb_valid.iloc[-1]))
                        except InsufficientDataError:
                            residual_beta = None

        results.append(
            HedgeCandidateOut(
                symbol=csym,
                beta=_clean(beta_cand),
                unusable=unusable,
                hedge_qty=_clean(hedge_qty),
                hedge_notional=_clean(hedge_notional),
                es_before=es_before,
                es_after=es_after,
                protection=_clean(protection),
                residual_beta=residual_beta,
                corr_stability=corr_stability,
                coint_pvalue=coint_pvalue,
            )
        )

    n_evaluated = len(results)
    if n_evaluated == 0:
        raise HTTPException(
            422,
            detail="no usable candidates: none of the candidate symbols had sufficient cached data",
        )

    # Rank by protection descending (Engineering Constraint 12: cointegration
    # is diagnostic only, never the ranking key); unusable/None-protection
    # candidates sort last but are still returned, flagged.
    results.sort(key=lambda r: (r.protection is None, -(r.protection if r.protection is not None else 0.0)))

    return HedgeResponse(
        benchmark=benchmark,
        objective=req.objective,
        book_value=_clean(book_value),
        book_beta=book_beta,
        es_before=es_before,
        n_candidates_evaluated=n_evaluated,
        candidates=results[:_MAX_CANDIDATES_OUT],
        as_of=_iso(book_prices.index[-1]) if len(book_prices) else None,
    )
