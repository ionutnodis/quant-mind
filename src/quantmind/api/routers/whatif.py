"""whatif domain routes: POST /api/whatif clones the book hypothetically and
recomputes risk side-by-side vs the live benchmark (DESIGN.md IA #5 — "clone
the book, modify, watch risk recompute"). Thin composition over the tested
pure core only (Global Constraints): `quantmind.risk.returns` for beta/ES/vol
and `quantmind.risk.montecarlo` for the block-bootstrap terminal distribution
— no math beyond wiring lives here.

Wave-3B "What-If flow" additions:

* `base_book_ref` — a pinned snapshot of the CURRENT book to diff against.
  The response then carries `base` (the current book's risk), `delta`
  (hypothetical − current) and `trade_ticket` (per-leg qty diffs keyed on
  the full leg: symbol/strike/expiry/right/multiplier).
* Common-random-numbers paired sims — base and hypothetical books simulate
  over the SAME aligned return panel with the SAME seed, so the block-
  bootstrap draws are identical draws and Δ statistics are noise-free
  (identical books ⇒ Δ exactly 0.0, the CRN identity). The shared seed is
  drawn once per request when the client doesn't post one, and is always
  echoed back in `mc.seed` so any run is replayable.
* Option legs (strike/expiry/right/multiplier on `PositionIn`, wave-3A) are
  wired through BOTH the inline-positions and book_ref paths. This engine is
  returns-based, so an option leg is priced as DELTA-ONE underlier notional
  (qty × multiplier × spot) — a declared approximation, surfaced in the
  response `notes`, never silent (Greeks-aware option risk lives in the
  options layer / book-greeks, not here).

Color adjudication (batch-1 final review): the CURRENT book (base) is the
user's book and renders amber; hypothetical/scenario values are NOT the live
book and render neutral, sign in the number (stress-grid precedent). This
router just supplies the honest numbers.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or insufficient overlap -> structured 422, never a 500 (pattern: routers/risk.py).
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.api.routers._shared import (
    PositionIn,
    _validate_option_legs,
    clean,
    iso,
    read_close_series,
    weighted_portfolio_returns,
)
from quantmind.api.routers.book import read_book, read_book_positions
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
_OPTION_NOTE = (
    "Option legs are priced as delta-one underlier notional (qty x multiplier x spot) "
    "in this returns-based engine — a declared approximation; Greeks-aware option risk "
    "lives in the options layer (book-greeks)."
)


class MonteCarloParams(BaseModel):
    horizon: int = Field(126, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = Field(None, ge=0, le=2**31 - 1)


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
    # The pinned CURRENT book to diff against (optional). Shape-validated by
    # routers/book.py's 12-hex snapshot-id regex on read; the length bound
    # here just rejects absurd inputs before they reach the resolver.
    base_book_ref: str | None = Field(None, min_length=1, max_length=64)
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
    # Leg descriptor (wave-3B option legs): null strike/expiry/right for a
    # plain equity/ETF leg; `multiplier` is always the EFFECTIVE multiplier
    # actually used in the exposure math (1.0 for a bare stock leg, 100
    # default for an option leg) so the inline and book_ref paths serialize
    # identically; `price` is always the UNDERLIER's last close.
    sec_type: Literal["STK", "OPT"] = "STK"
    strike: float | None = None
    expiry: str | None = None
    right: Literal["C", "P"] | None = None
    multiplier: float = 1.0
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
    # The shared CRN seed actually used (drawn once per request when the
    # client didn't post one) + the horizon it simulated — every risk number
    # is horizon-labeled and every run replayable.
    seed: int
    horizon_days: int


class BenchmarkOut(BaseModel):
    symbol: str
    es_975: float | None
    ann_vol: float | None


class BaseRiskOut(BaseModel):
    """The CURRENT (pinned) book's risk, computed on the SAME aligned panel
    and CRN seed as the hypothetical — the amber side of the comparison."""

    book_ref: str
    valuation_ts: str | None
    n_positions: int
    beta: float | None
    es_975: float | None
    ann_vol: float | None
    p5: float | None
    p50: float | None
    p95: float | None


class DeltaOut(BaseModel):
    """Hypothetical − current, CRN-paired (identical books ⇒ exactly 0.0)."""

    beta: float | None
    es_975: float | None
    ann_vol: float | None
    p5: float | None
    p50: float | None
    p95: float | None


class TicketLineOut(BaseModel):
    """One line of the current→hypothetical trade ticket: the qty change on
    a single leg, keyed on (symbol, strike, expiry, right, multiplier)."""

    symbol: str
    sec_type: Literal["STK", "OPT"]
    strike: float | None
    expiry: str | None
    right: Literal["C", "P"] | None
    multiplier: float
    qty_from: float
    qty_to: float
    qty_delta: float
    action: Literal["BUY", "SELL"]
    # Underlier last close (never an option premium — this engine has none).
    price: float | None


class WhatIfResponse(BaseModel):
    weights: list[WeightOut]
    beta: float | None
    es_975: float | None
    ann_vol: float | None
    mc: MonteCarloOut
    benchmark: BenchmarkOut
    n_obs: int
    as_of: str | None
    # Present only when `base_book_ref` was posted.
    base: BaseRiskOut | None = None
    delta: DeltaOut | None = None
    trade_ticket: list[TicketLineOut] | None = None
    # Declared approximations/caveats (e.g. the option delta-one proxy).
    notes: list[str] = Field(default_factory=list)


def _effective_multiplier(p: PositionIn) -> float:
    """_shared.PositionIn's convention: multiplier has no baked-in default so
    a bare equity leg is never silently 100x'd; an option leg (right set)
    defaults to the standard 100, anything else to 1.0 (a plain share)."""
    if p.multiplier is not None:
        return p.multiplier
    return 100.0 if p.right is not None else 1.0


def _book_exposures(positions: list[PositionIn], last_close: dict[str, float | None]) -> tuple[list[float], list[float]]:
    """(market_values, weights) for a book: delta-one exposure per leg
    (qty x effective multiplier x underlier last close), gross-normalized."""
    market_values = [p.qty * _effective_multiplier(p) * last_close[p.symbol] for p in positions]
    gross = sum(abs(mv) for mv in market_values)
    if gross <= 0:
        raise HTTPException(422, detail="portfolio has zero gross market value")
    return market_values, [mv / gross for mv in market_values]


def _book_risk(
    positions: list[PositionIn],
    weights_arr: np.ndarray,
    returns: pd.DataFrame,
    bench_returns: pd.Series,
    mc: MonteCarloParams,
    seed: int,
) -> tuple[float | None, float | None, float | None, np.ndarray]:
    """(beta, es_975, ann_vol, terminal returns) for one book over the SHARED
    aligned panel — both the base and hypothetical books go through this exact
    path with the same `seed` and the same `len(returns)`, which is what makes
    the bootstrap draws common random numbers."""
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

    # Monte Carlo terminal distribution over the SAME aligned per-leg returns
    # + weights (block bootstrap preserves cross-asset correlation), same
    # finite-guard shape as routers/risk.py and routers/lab.py.
    mc_returns_df = pd.DataFrame(
        {f"pos{i}": returns[p.symbol] for i, p in enumerate(positions)}, index=returns.index
    )
    terminal = simulate_terminal_returns(
        mc_returns_df,
        weights=weights_arr,
        n_paths=mc.n_paths,
        horizon=mc.horizon,
        seed=seed,
    )
    return beta, es, vol, terminal


def _leg_key(p: PositionIn) -> tuple:
    return (p.symbol, p.strike, p.expiry, p.right, _effective_multiplier(p))


def _trade_ticket(
    base_positions: list[PositionIn],
    hypo_positions: list[PositionIn],
    last_close: dict[str, float | None],
) -> list[TicketLineOut]:
    """Per-leg qty diff current→hypothetical. Legs are keyed on the FULL
    descriptor (symbol, strike, expiry, right, multiplier) so an option
    overlay is a new ticket line, never a qty change on the stock line."""
    base_qty: dict[tuple, float] = {}
    for p in base_positions:
        base_qty[_leg_key(p)] = base_qty.get(_leg_key(p), 0.0) + p.qty
    hypo_qty: dict[tuple, float] = {}
    for p in hypo_positions:
        hypo_qty[_leg_key(p)] = hypo_qty.get(_leg_key(p), 0.0) + p.qty

    lines: list[TicketLineOut] = []
    for key in sorted(
        set(base_qty) | set(hypo_qty),
        key=lambda k: (k[0], k[2] or "", k[1] or 0.0, k[3] or ""),
    ):
        symbol, strike, expiry, right, multiplier = key
        q_from = base_qty.get(key, 0.0)
        q_to = hypo_qty.get(key, 0.0)
        q_delta = q_to - q_from
        if q_delta == 0.0:
            continue
        lines.append(
            TicketLineOut(
                symbol=symbol,
                sec_type="OPT" if right is not None else "STK",
                strike=clean(strike),
                expiry=expiry,
                right=right,
                multiplier=multiplier,
                qty_from=q_from,
                qty_to=q_to,
                qty_delta=q_delta,
                action="BUY" if q_delta > 0 else "SELL",
                price=clean(last_close.get(symbol)),
            )
        )
    return lines


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
    _validate_option_legs(positions, "book")

    # The base (CURRENT) book to diff against, if any. An EMPTY base book is
    # legitimate (no broker configured -> /api/book/current pins an empty
    # book): the ticket then opens every hypothetical leg, and base risk is
    # honestly null rather than fabricated.
    base_positions: list[PositionIn] | None = None
    base_valuation_ts: str | None = None
    if req.base_book_ref is not None:
        base_payload = read_book(store, req.base_book_ref)
        base_valuation_ts = base_payload.get("valuation_ts")
        base_positions = read_book_positions(store, req.base_book_ref)
        if len(base_positions) > _MAX_POSITIONS:
            raise HTTPException(
                422, detail=f"base book has {len(base_positions)} positions; max {_MAX_POSITIONS}"
            )
        _validate_option_legs(base_positions, "base book")

    requested = [p.symbol for p in positions]
    base_symbols = [p.symbol for p in (base_positions or [])]
    unique_needed = list(dict.fromkeys([*requested, *base_symbols]))
    unknown = sorted(s for s in unique_needed if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")
    if benchmark not in symbol_map:
        raise HTTPException(422, detail=f"benchmark {benchmark!r} not in cache")

    # Inner-join every symbol involved (both books' legs + benchmark) on
    # trading dates: portfolio daily returns are only well-defined where
    # every leg (and the benchmark, for beta) has a price — and ONE shared
    # panel is what makes the base/hypothetical sims common-random-number
    # paired (same n_days ⇒ same bootstrap block starts for the same seed).
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

    market_values, weight_values = _book_exposures(positions, last_close)

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

    # ONE shared seed per request (CRN): posted seed if any, else drawn once
    # here — both books' bootstrap draws use it, and it's echoed in mc.seed.
    shared_seed = (
        req.mc.seed
        if req.mc.seed is not None
        else int(np.random.default_rng().integers(0, 2**31 - 1))
    )

    weights_arr = np.array(weight_values)
    beta, es, vol, terminal = _book_risk(positions, weights_arr, returns, bench_returns, req.mc, shared_seed)

    try:
        bench_es = clean(historical_es(bench_returns, confidence=0.975))
    except InsufficientDataError:
        bench_es = None

    try:
        bench_vol = clean(annualized_vol(bench_returns))
    except InsufficientDataError:
        bench_vol = None

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

    # Base (current) book on the SAME panel + seed; hypothetical − base is
    # then noise-free (the CRN identity: identical books ⇒ Δ exactly 0.0).
    base_out: BaseRiskOut | None = None
    delta_out: DeltaOut | None = None
    ticket_out: list[TicketLineOut] | None = None
    if base_positions is not None:
        ticket_out = _trade_ticket(base_positions, positions, last_close)
        if base_positions:
            _, base_weight_values = _book_exposures(base_positions, last_close)
            base_beta, base_es, base_vol, base_terminal = _book_risk(
                base_positions, np.array(base_weight_values), returns, bench_returns, req.mc, shared_seed
            )
            base_finite = base_terminal[np.isfinite(base_terminal)]
            if len(base_finite):
                base_p5, base_p50, base_p95 = (
                    clean(float(x)) for x in np.percentile(base_finite, [5, 50, 95])
                )
            else:
                base_p5 = base_p50 = base_p95 = None
            base_out = BaseRiskOut(
                book_ref=req.base_book_ref,
                valuation_ts=base_valuation_ts,
                n_positions=len(base_positions),
                beta=base_beta,
                es_975=base_es,
                ann_vol=base_vol,
                p5=base_p5,
                p50=base_p50,
                p95=base_p95,
            )

            def _d(a: float | None, b: float | None) -> float | None:
                return clean(a - b) if a is not None and b is not None else None

            delta_out = DeltaOut(
                beta=_d(beta, base_beta),
                es_975=_d(es, base_es),
                ann_vol=_d(vol, base_vol),
                p5=_d(clean(p5), base_p5),
                p50=_d(clean(p50), base_p50),
                p95=_d(clean(p95), base_p95),
            )
        else:
            base_out = BaseRiskOut(
                book_ref=req.base_book_ref,
                valuation_ts=base_valuation_ts,
                n_positions=0,
                beta=None,
                es_975=None,
                ann_vol=None,
                p5=None,
                p50=None,
                p95=None,
            )

    notes: list[str] = []
    if any(p.right is not None for p in [*positions, *(base_positions or [])]):
        notes.append(_OPTION_NOTE)

    weights_out = [
        WeightOut(
            symbol=p.symbol,
            qty=p.qty,
            sec_type="OPT" if p.right is not None else "STK",
            strike=clean(p.strike),
            expiry=p.expiry,
            right=p.right,
            multiplier=_effective_multiplier(p),
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
            seed=shared_seed,
            horizon_days=req.mc.horizon,
        ),
        benchmark=BenchmarkOut(symbol=benchmark, es_975=bench_es, ann_vol=bench_vol),
        n_obs=len(returns),
        as_of=iso(prices.index[-1]) if len(prices) else None,
        base=base_out,
        delta=delta_out,
        trade_ticket=ticket_out,
        notes=notes,
    )
