"""hedge domain routes — the Hedge Lab (DESIGN.md IA #4): "decisions, not
analytics." POST /api/hedge takes a book + a beta_target objective and
returns candidates ranked by protection (ES reduction), sized to move the
book's beta to target.

Cointegration diagnostic removed (pre-wave-3 consolidation pass, TODOS.md):
Engle-Granger p-value used to ride along as a labeled DIAGNOSTIC column here
(Engineering Constraint 12 — never the ranking key), but its home is Lab's
pair pipeline (wave-3B), not the Hedge Lab. Ranking is — and was always —
strictly by protection; removing the column drops a broad `except Exception`
around the Engle-Granger/ADF machinery from this router entirely rather than
narrowing it, since the call itself is gone.

Thin composition over the tested pure core only (Global Constraints):
quantmind.risk.returns for beta/ES, quantmind.analytics.correlation for the
rolling correlation-stability diagnostic. No math beyond wiring lives here.

Alignment approach mirrors routers/whatif.py: price-level inner join across
every symbol involved, then pct_change, weights by |market value|-signed.
Degenerate-input handling also mirrors whatif.py (pre-wave-3 consolidation
pass): a non-finite last close in the BOOK's cached bars, or a book whose
gross market value is zero, is a named 422 — never a silent NaN.

Normalization convention (es_before vs es_after MUST share one denominator):
the hedge candidate is priced as an OVERLAY on the original book, never
folded into a re-normalized blended portfolio. Concretely, `book_returns` is
a per-original-book-dollar return series (weights are fractions of the
ORIGINAL book's gross). The hedge leg's daily dollar P&L is approximated as
`hedge_notional * cand_return(t)` (constant-notional approximation over the
window) and is converted to the SAME per-original-book-dollar units by
dividing by that same original gross:
`hedged_return(t) = book_return(t) + hedge_notional * cand_return(t) / book_gross`.
es_after = historical_es(hedged_returns) then shares es_before's denominator
exactly. The earlier approach re-ran `_portfolio_returns` on book+hedge
together, which re-normalizes weights by the NEW (inflated) gross whenever
the hedge notional is large — mechanically shrinking the hedge leg's weight
and deflating es_after for large-notional hedges (e.g. a low-beta candidate
that needs a huge notional to hit the target), biasing `protection` upward
for exactly the candidates that should look worst. The overlay convention
above removes that bias: protection can only come from the hedge actually
reducing tail risk, not from denominator inflation.

Hedge sizing: to move book beta from `book_beta` to `objective.value` by
adding `hedge_qty` shares of a candidate with beta `beta_cand` at price
`price_cand`, the dollar-beta needed from the hedge leg is
`(objective.value - book_beta) * book_value`, so
`hedge_qty = (objective.value - book_beta) * book_value / (beta_cand * price_cand)`
`= -(book_beta - objective.value) * book_value / (beta_cand * price_cand)`.
A candidate with |beta| < 0.1 is flagged `unusable` (sizing would blow up)
and reported without a size/protection, never dropped from the response.

`book_ref` (wave-3 Task A1's book-flow spine): the `book` field accepts a
pinned snapshot id instead of inline positions — see routers/book.py and
routers/whatif.py's identical `book_ref` handling.

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
from pydantic import BaseModel, Field, model_validator

from quantmind.analytics.correlation import rolling_correlation
from quantmind.api.routers._shared import PositionIn, clean, iso, read_close_series, weighted_portfolio_returns
from quantmind.api.routers.book import read_book_positions
from quantmind.hedge.core import diversification_ratio, leverage_headroom, max_drawdown
from quantmind.risk.returns import InsufficientDataError, historical_es, rolling_beta

router = APIRouter()

_BETA_WINDOW = 60
_MIN_BETA_ABS = 0.1
_MAX_CANDIDATES_OUT = 20
_MAX_BOOK_POSITIONS = 50


class Objective(BaseModel):
    kind: Literal["beta_target"] = "beta_target"
    value: float = Field(..., ge=-2.0, le=2.0)


class HedgeRequest(BaseModel):
    # Exactly one of `book` (inline positions) or `book_ref` (a pinned
    # snapshot id, wave-3 Task A1) must be given — see `_book_xor_book_ref`.
    book: list[PositionIn] | None = Field(None, min_length=1, max_length=50)
    book_ref: str | None = None
    objective: Objective
    # Default = the cached universe minus book symbols (resolved in-handler,
    # request.app.state.store isn't available at model-validation time).
    candidates: list[str] | None = None
    years: int = Field(5, ge=1, le=25)

    @model_validator(mode="after")
    def _book_xor_book_ref(self) -> "HedgeRequest":
        if bool(self.book) == bool(self.book_ref):
            raise ValueError("provide exactly one of book or book_ref")
        return self


class LeverageRequest(BaseModel):
    book: list[PositionIn] | None = Field(None, min_length=1, max_length=50)
    book_ref: str | None = None
    # Target worst-case drawdown the book should be sized to (e.g. 0.25 = 25%).
    drawdown_budget: float = Field(0.25, gt=0.0, le=1.0)
    years: int = Field(5, ge=1, le=25)

    @model_validator(mode="after")
    def _book_xor_book_ref(self) -> "LeverageRequest":
        if bool(self.book) == bool(self.book_ref):
            raise ValueError("provide exactly one of book or book_ref")
        return self


class LeverageResponse(BaseModel):
    symbols: list[str]
    n_obs: int
    max_drawdown: float | None
    drawdown_budget: float
    leverage_headroom: float | None
    diversification_ratio: float | None
    book_value: float | None
    gross: float | None
    note: str
    as_of: str | None


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


class HedgeResponse(BaseModel):
    benchmark: str
    objective: Objective
    book_value: float | None
    book_beta: float | None
    es_before: float | None
    n_candidates_evaluated: int
    candidates: list[HedgeCandidateOut]
    as_of: str | None


def _portfolio_returns(
    series_map: dict[str, pd.Series], qtys: dict[str, float], symbols: list[str]
) -> tuple[pd.Series | None, dict[str, float], float, float, pd.DataFrame]:
    """Price-level inner join across `symbols`, then pct_change, weighted by
    |market value|-signed weight (mirrors routers/whatif.py's alignment).
    Returns (portfolio_returns, weights, book_value, gross, prices); `gross`
    is the denominator the caller must reuse for any per-book-dollar overlay
    computation (see module docstring's normalization convention)."""
    last_close = {s: float(series_map[s].iloc[-1]) for s in symbols}
    market_values = {s: qtys[s] * last_close[s] for s in symbols}
    gross = sum(abs(v) for v in market_values.values())
    weights = {s: (market_values[s] / gross if gross else 0.0) for s in symbols}
    book_value = sum(market_values.values())

    prices = pd.concat({s: series_map[s] for s in symbols}, axis=1).dropna()
    returns = prices.pct_change().dropna()
    if len(returns) == 0:
        return None, weights, book_value, gross, prices
    weights_arr = np.array([weights[s] for s in symbols])
    portfolio_returns = weighted_portfolio_returns(returns, symbols, weights_arr)
    return portfolio_returns, weights, book_value, gross, prices


@router.post("/hedge", response_model=HedgeResponse)
def hedge(request: Request, req: HedgeRequest) -> HedgeResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    symbol_map = store.read_symbol_map()

    # book_ref resolves to the same PositionIn shape as an inline book
    # (read_book_positions 422s naming the ref if it's unknown); Field's
    # min_length/max_length=1..50 only runs on an inline `book` body, so a
    # book_ref-resolved list gets the same bounds check by hand here.
    book_positions = req.book if req.book is not None else read_book_positions(store, req.book_ref)
    if not book_positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(book_positions) > _MAX_BOOK_POSITIONS:
        raise HTTPException(422, detail=f"book has {len(book_positions)} positions; max {_MAX_BOOK_POSITIONS}")

    unique_book = list(dict.fromkeys(p.symbol for p in book_positions))
    qtys: dict[str, float] = {}
    for p in book_positions:
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
        series_map[sym] = read_close_series(store, symbol_map[sym], sym, req.years)

    # NaN/Inf last close (corrupted/partial sync data) makes a book leg
    # unpriceable — named 422 rather than a silently propagated NaN, aligned
    # with routers/whatif.py's identical guard (pre-wave-3 consolidation
    # pass: this check was previously missing here).
    unpriceable = sorted(sym for sym in unique_book if clean(float(series_map[sym].iloc[-1])) is None)
    if unpriceable:
        raise HTTPException(
            422,
            detail=(
                f"non-finite last close in cached bars for: {unpriceable} — "
                "re-sync before computing"
            ),
        )

    book_returns, _weights, book_value, book_gross, book_prices = _portfolio_returns(series_map, qtys, unique_book)
    if book_gross <= 0:
        raise HTTPException(422, detail="portfolio has zero gross market value")
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
        book_beta = clean(float(beta_valid.iloc[-1])) if len(beta_valid) else None
    except InsufficientDataError:
        book_beta = None

    try:
        es_before = clean(historical_es(book_returns, confidence=0.975))
    except InsufficientDataError:
        es_before = None

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
            cand_prices = read_close_series(store, symbol_map[csym], csym, req.years)
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
                corr_stability = clean(float(roll_corr.std()))

        unusable = beta_cand is None or not math.isfinite(beta_cand) or abs(beta_cand) < _MIN_BETA_ABS

        hedge_qty = hedge_notional = es_after = protection = residual_beta = None

        if not unusable and book_beta is not None:
            price_cand_last = float(cand_prices.iloc[-1])
            if math.isfinite(price_cand_last) and price_cand_last != 0:
                raw_size = (book_beta - req.objective.value) * book_value / (beta_cand * price_cand_last)
                hedge_qty = -raw_size
                hedge_notional = hedge_qty * price_cand_last

                # Overlay, not a re-blended portfolio (see module docstring's
                # normalization convention): the hedge leg's daily dollar P&L
                # (hedge_notional * cand_return(t), constant-notional
                # approximation) is added to book_returns after converting to
                # the SAME per-original-book-dollar units via book_gross —
                # never the (possibly hedge-inflated) new portfolio gross.
                aligned_overlay = pd.concat({"book": book_returns, "cand": cand_returns}, axis=1).dropna()
                hedged_returns: pd.Series | None = None
                if len(aligned_overlay) > 0 and book_gross:
                    hedge_leg_returns = hedge_notional * aligned_overlay["cand"] / book_gross
                    hedged_returns = aligned_overlay["book"] + hedge_leg_returns

                if hedged_returns is not None:
                    try:
                        es_after = clean(historical_es(hedged_returns, confidence=0.975))
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
                                residual_beta = clean(float(rb_valid.iloc[-1]))
                        except InsufficientDataError:
                            residual_beta = None

        results.append(
            HedgeCandidateOut(
                symbol=csym,
                beta=clean(beta_cand),
                unusable=unusable,
                hedge_qty=clean(hedge_qty),
                hedge_notional=clean(hedge_notional),
                es_before=es_before,
                es_after=es_after,
                protection=clean(protection),
                residual_beta=residual_beta,
                corr_stability=corr_stability,
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
        book_value=clean(book_value),
        book_beta=book_beta,
        es_before=es_before,
        n_candidates_evaluated=n_evaluated,
        candidates=results[:_MAX_CANDIDATES_OUT],
        as_of=iso(book_prices.index[-1]) if len(book_prices) else None,
    )


@router.post("/leverage", response_model=LeverageResponse)
def leverage(request: Request, req: LeverageRequest) -> LeverageResponse:
    """Resilience construction: the book's historical max drawdown, the
    drawdown-budget leverage headroom (assumption-bound scenario leverage, NOT a
    safe-leverage guarantee — H4), and the diversification ratio (how orthogonal
    the legs are). Thin composition over quantmind.hedge.core."""
    store = request.app.state.store
    symbol_map = store.read_symbol_map()

    book_positions = req.book if req.book is not None else read_book_positions(store, req.book_ref)
    if not book_positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(book_positions) > _MAX_BOOK_POSITIONS:
        raise HTTPException(422, detail=f"book has {len(book_positions)} positions; max {_MAX_BOOK_POSITIONS}")

    unique_book = list(dict.fromkeys(p.symbol for p in book_positions))
    qtys: dict[str, float] = {}
    for p in book_positions:
        qtys[p.symbol] = qtys.get(p.symbol, 0.0) + p.qty

    unknown = sorted(s for s in unique_book if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")

    series_map = {sym: read_close_series(store, symbol_map[sym], sym, req.years) for sym in unique_book}
    # Non-finite last close -> a NaN gross would slip past `gross <= 0` (NaN<=0
    # is False) and return a 200 of nulls; name the 422 instead (mirrors /hedge).
    unpriceable = sorted(sym for sym in unique_book if clean(float(series_map[sym].iloc[-1])) is None)
    if unpriceable:
        raise HTTPException(
            422, detail=f"non-finite last close in cached bars for: {unpriceable} — re-sync before computing"
        )
    book_returns, weights, book_value, gross, prices = _portfolio_returns(series_map, qtys, unique_book)
    if gross <= 0:
        raise HTTPException(422, detail="portfolio has zero gross market value")
    if book_returns is None or len(book_returns) < 2:
        raise HTTPException(422, detail="insufficient overlapping history for the book")

    try:
        mdd: float | None = max_drawdown(book_returns)
    except InsufficientDataError:
        mdd = None
    try:
        headroom: float | None = leverage_headroom(book_returns, req.drawdown_budget) if mdd is not None else None
    except ValueError:
        headroom = None  # no historical drawdown -> headroom undefined, not a 500

    per_symbol = prices.pct_change().dropna()
    try:
        div_ratio: float | None = diversification_ratio(
            per_symbol, np.array([weights[s] for s in unique_book])
        )
    except InsufficientDataError:
        div_ratio = None  # single instrument or degenerate -> undefined

    return LeverageResponse(
        symbols=unique_book,
        n_obs=len(book_returns),
        max_drawdown=clean(mdd),
        drawdown_budget=req.drawdown_budget,
        leverage_headroom=clean(headroom),
        diversification_ratio=clean(div_ratio),
        book_value=clean(book_value),
        gross=clean(gross),
        note=(
            "leverage headroom is assumption-bound scenario leverage (scales historical "
            "drawdown; ignores margin/gap/options nonlinearity) — not a safe-leverage "
            "guarantee. Equity sleeve, per gross dollar."
        ),
        as_of=iso(prices.index[-1]) if len(prices) else None,
    )
