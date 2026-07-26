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

Wave-3B "Hedge honest" (docs/plans/2026-07-25-wave3.md Batch 2): the page is
honest about cost and uncertainty. All math is pure and golden-tested in
src/quantmind/hedge/ — this router only wires it:
- Cost columns per candidate: carry drag (β_h · E[r_bench], annualized from
  the SAME cached bars/window) + a borrow-fee PROXY constant on short/inverse
  notional (hedge/cost.py). Fractions of the original book's gross per year.
- Ranking key is protection-per-cost (ΔES per unit of annual drag); a
  candidate whose cost is non-positive (credit/tailwind) has no meaningful
  ratio, so it falls back to raw-protection ordering after the costed ones.
  Unusable candidates still sort last, still flagged, never dropped.
- ΔES carries a 95% CI from a seeded PAIRED block bootstrap
  (hedge/bootstrap.py, mirroring risk/montecarlo.py's block sampling) —
  wave-3 Global Constraint: any bootstrap statistic shows its interval.
- Tail-conditional protection: mean DAILY book return on the worst-decile
  benchmark days in the window, with vs without each hedge (hedge/tail.py).
- Option hedge candidates (protective put / put spread / collar) on the
  book's DOMINANT underlier (largest |market value|), built from the CACHED
  chain snapshot (OptionsStore — never a live IB call), sized off
  risk/options.py's stress grid at the -20% node, premium expressed as an
  annual % drag over time-to-expiry (hedge/option_hedges.py). A missing or
  empty chain (SPY is empty upstream today) degrades to a structured
  `option_note`, never a 500. The ES overlay uses THE SAME
  original-book-gross denominator as the linear candidates.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or an empty candidate universe -> structured 422, never a 500 (pattern:
routers/risk.py, routers/whatif.py).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.analytics.correlation import rolling_correlation
from quantmind.api.routers._shared import PositionIn, clean, iso, read_close_series, weighted_portfolio_returns
from quantmind.api.routers.book import read_book_positions
from quantmind.datastore.options_store import OptionsStore
from quantmind.hedge.bootstrap import delta_es_ci
from quantmind.hedge.cost import (
    BORROW_PROXY_RATE,
    annualized_mean_return,
    borrow_proxy_annual,
    carry_drag_annual,
    protection_per_cost,
)
from quantmind.hedge.option_hedges import (
    OptionStructure,
    build_structures,
    premium_annual_drag,
    size_contracts,
    structure_daily_pnl,
)
from quantmind.hedge.tail import worst_decile_tail
from quantmind.risk.returns import InsufficientDataError, historical_es, rolling_beta

router = APIRouter()

_BETA_WINDOW = 60
_MIN_BETA_ABS = 0.1
_MAX_CANDIDATES_OUT = 20
_MAX_BOOK_POSITIONS = 50

# ΔES CI: seeded paired block bootstrap (hedge/bootstrap.py). The fixed seed
# keeps identical requests byte-identical (book_ref-vs-inline equality is a
# tested contract).
_CI_N_BOOT = 500
_CI_BLOCK_SIZE = 5
_CI_SEED = 0
_TAIL_DECILE = 0.10
# Option-structure sizing node: the stress grid's standard worst spot shock.
_OPTION_SHOCK = -0.20
_OPTION_MIN_DAYS = 20


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


class HedgeCandidateOut(BaseModel):
    symbol: str
    beta: float | None
    unusable: bool
    hedge_qty: float | None
    hedge_notional: float | None
    es_before: float | None
    es_after: float | None
    protection: float | None
    # Wave-3B "Hedge honest" — cost + uncertainty columns. All fractions of
    # the ORIGINAL book's gross; cost fields are per YEAR, ES/tail are DAILY.
    carry_drag_annual: float | None
    borrow_proxy_annual: float | None
    cost_annual: float | None
    protection_per_cost: float | None  # THE ranking key: ΔES per unit annual drag
    delta_es_ci_low: float | None
    delta_es_ci_high: float | None
    tail_n_days: int | None
    tail_mean_book: float | None
    tail_mean_hedged: float | None
    residual_beta: float | None
    # Diagnostic only (Engineering Constraint 12) — never the ranking key.
    corr_stability: float | None


class OptionLegOut(BaseModel):
    action: Literal["long", "short"]
    # Nullable purely as NaN->null insurance (fix round 1): chain selection
    # refuses non-finite strikes/prices, and clean() here guarantees a NaN
    # can never serialize as an invalid JSON literal.
    strike: float | None
    right: Literal["C", "P"]
    price: float | None  # per-share premium at the traded side (ask long / bid short)


class OptionHedgeOut(BaseModel):
    kind: Literal["protective_put", "put_spread", "collar"]
    expiry: str  # YYYYMMDD
    expiry_years: float | None
    legs: list[OptionLegOut]
    contracts: float | None
    net_premium_per_contract: float | None  # dollars per structure
    cost_annual: float | None  # premium as % annual drag (fraction of gross / yr)
    es_before: float | None
    es_after: float | None
    protection: float | None
    protection_per_cost: float | None
    delta_es_ci_low: float | None
    delta_es_ci_high: float | None
    tail_n_days: int | None
    tail_mean_book: float | None
    tail_mean_hedged: float | None


class HedgeResponse(BaseModel):
    benchmark: str
    objective: Objective
    book_value: float | None
    book_beta: float | None
    es_before: float | None
    # E[r_bench] annualized from the cached bars over the request window —
    # the carry-drag factor, surfaced so the cost column is auditable.
    bench_expected_return_annual: float | None
    n_candidates_evaluated: int
    candidates: list[HedgeCandidateOut]
    # Option hedge candidates on the dominant underlier; empty + option_note
    # when the chain is missing/empty or the sleeve is short (never a 500).
    option_underlier: str | None
    option_chain_as_of: str | None
    option_hedges: list[OptionHedgeOut]
    option_note: str | None
    # Methodology/horizon labels (wave-3 Global Constraint: every risk number
    # is horizon-labeled; the borrow constant is a labeled proxy).
    es_note: str
    cost_note: str
    ci_note: str
    tail_note: str
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

    # E[r_bench] for the carry-drag column, annualized from the SAME cached
    # bars/window everything else here uses (hedge/cost.py).
    er_bench = clean(annualized_mean_return(bench_returns)) if len(bench_returns) else None

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
        es_before_aligned = None
        carry_drag = borrow_proxy = cost_annual = ppc = None
        ci_low = ci_high = None
        tail_n_days = tail_mean_book = tail_mean_hedged = None

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

                    # Window consistency (fix round 1): the DISPLAYED ΔES must
                    # be computed on the SAME book∩candidate window as
                    # es_after, the bootstrap CI and the tail stats. Using the
                    # full-window es_before here manufactured phantom
                    # protection for any candidate with shorter cached history
                    # (the truncated window simply misses the book's worst
                    # days) — a Δ that could fall entirely outside its own CI
                    # and poison protection_per_cost, THE ranking key. The
                    # full-window es_before remains the response-level
                    # headline only.
                    try:
                        es_before_aligned = clean(
                            historical_es(aligned_overlay["book"], confidence=0.975)
                        )
                    except InsufficientDataError:
                        es_before_aligned = None
                    if es_before_aligned is not None and es_after is not None:
                        protection = es_before_aligned - es_after

                    # Cost columns (hedge/cost.py): carry drag + borrow
                    # proxy, fractions of the ORIGINAL gross per year.
                    if er_bench is not None:
                        carry_drag = clean(carry_drag_annual(hedge_notional, beta_cand, er_bench, book_gross))
                        borrow_proxy = clean(borrow_proxy_annual(hedge_notional, beta_cand, book_gross))
                        if carry_drag is not None and borrow_proxy is not None:
                            cost_annual = carry_drag + borrow_proxy
                    ppc = protection_per_cost(protection, cost_annual)

                    # ΔES 95% CI: seeded PAIRED block bootstrap.
                    ci = delta_es_ci(
                        aligned_overlay["book"].to_numpy(),
                        hedged_returns.to_numpy(),
                        n_boot=_CI_N_BOOT,
                        block_size=_CI_BLOCK_SIZE,
                        seed=_CI_SEED,
                    )
                    if ci is not None:
                        ci_low, ci_high = clean(ci[0]), clean(ci[1])

                    # Tail-conditional protection: worst-decile bench days.
                    tail = worst_decile_tail(
                        aligned_overlay["book"], hedged_returns, bench_returns, decile=_TAIL_DECILE
                    )
                    if tail is not None:
                        tail_n_days = tail.n_days
                        tail_mean_book = clean(tail.mean_book)
                        tail_mean_hedged = clean(tail.mean_hedged)

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
                # Window-consistent (fix round 1): the candidate row's
                # es_before shares es_after/CI/tail's book∩candidate window;
                # the full-window number is the response-level headline.
                es_before=es_before_aligned,
                es_after=es_after,
                protection=clean(protection),
                carry_drag_annual=carry_drag,
                borrow_proxy_annual=borrow_proxy,
                cost_annual=clean(cost_annual),
                protection_per_cost=clean(ppc),
                delta_es_ci_low=ci_low,
                delta_es_ci_high=ci_high,
                tail_n_days=tail_n_days,
                tail_mean_book=tail_mean_book,
                tail_mean_hedged=tail_mean_hedged,
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

    # Rank by protection-per-cost descending (wave-3B "Hedge honest");
    # candidates without a meaningful ratio (credit/tailwind cost, or no
    # protection at all) fall back to raw-protection ordering after the
    # costed ones; unusable candidates sort last but are still returned,
    # flagged. (Engineering Constraint 12 still holds: correlation stability
    # is diagnostic only, never the ranking key.)
    results.sort(key=lambda r: _rank_key(r.protection_per_cost, r.protection))

    # Option hedge candidates on the dominant underlier (largest |mv| book
    # leg) from the CACHED chain — structured note on any degrade.
    last_close = {s: float(series_map[s].iloc[-1]) for s in unique_book}
    dominant = max(unique_book, key=lambda s: abs(qtys[s] * last_close[s]))
    option_hedges, option_note, option_chain_as_of = _build_option_hedges(
        store=store,
        dominant=dominant,
        mv_dominant=qtys[dominant] * last_close[dominant],
        dominant_prices=series_map[dominant],
        book_returns=book_returns,
        bench_returns=bench_returns,
        book_gross=book_gross,
    )
    option_hedges.sort(key=lambda o: _rank_key(o.protection_per_cost, o.protection))

    return HedgeResponse(
        benchmark=benchmark,
        objective=req.objective,
        book_value=clean(book_value),
        book_beta=book_beta,
        es_before=es_before,
        bench_expected_return_annual=er_bench,
        n_candidates_evaluated=n_evaluated,
        candidates=results[:_MAX_CANDIDATES_OUT],
        option_underlier=dominant,
        option_chain_as_of=option_chain_as_of,
        option_hedges=option_hedges,
        option_note=option_note,
        es_note=(
            f"ES = historical expected shortfall (97.5%) of DAILY returns over the "
            f"{req.years}y window, as a fraction of book gross"
        ),
        cost_note=(
            f"cost/yr = carry drag (β_h · E[r_bench]; E[r_bench] = "
            f"{er_bench:+.2%}/yr from cached daily bars over the window) "
            f"+ borrow proxy {BORROW_PROXY_RATE:.2%}/yr on short/inverse notional "
            f"(a labeled PROXY, not a quoted borrow rate); option premium annualized "
            f"over time-to-expiry; all fractions of book gross per year"
            if er_bench is not None
            else "cost/yr unavailable: benchmark expected return could not be estimated"
        ),
        ci_note=(
            f"ΔES interval = 95% CI from a seeded paired block bootstrap "
            f"(block={_CI_BLOCK_SIZE}, n={_CI_N_BOOT}) of daily returns"
        ),
        tail_note=(
            f"tail panel = mean DAILY book return on the worst-decile {benchmark} "
            f"days in the window, with vs without each hedge"
        ),
        as_of=iso(book_prices.index[-1]) if len(book_prices) else None,
    )


def _rank_key(ppc: float | None, protection: float | None) -> tuple:
    """Sort key shared by linear and option candidates: protection-per-cost
    desc, then (for un-costed candidates) protection desc, unusable last."""
    return (
        ppc is None,
        -(ppc if ppc is not None else 0.0),
        protection is None,
        -(protection if protection is not None else 0.0),
    )


def _chain_as_of_date(as_of: str) -> date:
    """The chain snapshot's date, for time-to-expiry: option premiums/IVs are
    only honest relative to WHEN they were snapped, not to today."""
    try:
        return datetime.strptime(as_of[:10], "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _build_option_hedges(
    store,
    dominant: str,
    mv_dominant: float,
    dominant_prices: pd.Series,
    book_returns: pd.Series,
    bench_returns: pd.Series,
    book_gross: float,
) -> tuple[list[OptionHedgeOut], str | None, str | None]:
    """(option_hedges, option_note, chain_as_of): protective structures on
    the dominant underlier from the cached chain. Every degrade path returns
    a structured note — never raises (never-500)."""
    if mv_dominant <= 0:
        return (
            [],
            f"dominant underlier {dominant} is a short/zero position — protective "
            "put/spread/collar structures apply to a long sleeve only",
            None,
        )

    options_store = OptionsStore(store.root)
    if not options_store.has_chain(dominant):
        return (
            [],
            f"no cached option chain for {dominant} — run options_sync_cli to snapshot "
            "one (only synced underliers, e.g. QQQ, have chains today)",
            None,
        )
    try:
        chain_df, meta = options_store.read_chain(dominant)
    except FileNotFoundError:
        # TOCTOU-safe: the file vanished between has_chain and read_chain.
        return [], f"no cached option chain for {dominant}", None

    as_of_date = _chain_as_of_date(meta.as_of)
    spot = meta.spot if math.isfinite(meta.spot) and meta.spot > 0 else float(dominant_prices.iloc[-1])

    structures, notes = build_structures(chain_df, spot=spot, as_of=as_of_date, min_days=_OPTION_MIN_DAYS)
    if not structures:
        return [], "; ".join(notes) if notes else "no option structures could be built", meta.as_of

    dom_returns = dominant_prices.pct_change().dropna()
    aligned = pd.concat({"book": book_returns, "dom": dom_returns}, axis=1).dropna()
    # Distinct degrade cause (fix round 1): an empty overlap window is a DATA
    # gap, not a payoff property of the structures — say so honestly instead
    # of blaming the stress node.
    if len(aligned) == 0:
        return (
            [],
            f"no overlapping trading days between the book's return window and "
            f"{dominant}'s cached bars — option overlay unavailable",
            meta.as_of,
        )

    out: list[OptionHedgeOut] = []
    for st in structures:
        entry = _price_structure(st, mv_dominant, spot, aligned, bench_returns, book_gross)
        if entry is None:
            notes.append(f"{st.kind}: no payoff at the {_OPTION_SHOCK:+.0%} stress node — unsized")
            continue
        out.append(entry)

    method = (
        f"sized off the {_OPTION_SHOCK:+.0%} stress-grid node of the {dominant} sleeve; "
        "overlay repriced at constant time-to-expiry and IV (theta/vol response are "
        "carried by the premium-drag cost column, not the ES overlay); premiums pay "
        "the spread (long at ask, short at bid)"
    )
    note = "; ".join([method, *notes]) if out else ("; ".join(notes) if notes else None)
    return out, note, meta.as_of


def _price_structure(
    st: OptionStructure,
    mv_dominant: float,
    spot: float,
    aligned: pd.DataFrame,
    bench_returns: pd.Series,
    book_gross: float,
) -> OptionHedgeOut | None:
    """None means exactly one thing: the sized structure has no payoff at the
    stress node (the caller's note says so). The empty-overlap case is
    handled — and named — by the caller before this runs (fix round 1)."""
    contracts = size_contracts(st, mv_underlier=mv_dominant, spot=spot, shock=_OPTION_SHOCK)
    if contracts is None:
        return None

    pnl = structure_daily_pnl(st, contracts=contracts, spot=spot, underlier_returns=aligned["dom"])
    # SAME overlay convention as the linear candidates: dollar P&L divided by
    # the ORIGINAL book's gross, added to per-original-book-dollar returns.
    hedged_returns = aligned["book"] + pnl / book_gross

    try:
        es_after = clean(historical_es(hedged_returns, confidence=0.975))
    except InsufficientDataError:
        es_after = None
    # Window consistency (fix round 1): same discipline as the linear
    # candidates — the displayed ΔES shares es_after/CI/tail's book∩dominant
    # window, never the full-window headline.
    try:
        es_before_aligned = clean(historical_es(aligned["book"], confidence=0.975))
    except InsufficientDataError:
        es_before_aligned = None
    protection = (
        es_before_aligned - es_after
        if es_before_aligned is not None and es_after is not None
        else None
    )

    cost_annual = clean(
        premium_annual_drag(st.net_premium_per_contract, contracts, book_gross, st.expiry_years)
    )
    ppc = protection_per_cost(protection, cost_annual)

    ci = delta_es_ci(
        aligned["book"].to_numpy(),
        hedged_returns.to_numpy(),
        n_boot=_CI_N_BOOT,
        block_size=_CI_BLOCK_SIZE,
        seed=_CI_SEED,
    )
    tail = worst_decile_tail(aligned["book"], hedged_returns, bench_returns, decile=_TAIL_DECILE)

    return OptionHedgeOut(
        kind=st.kind,
        expiry=st.expiry,
        expiry_years=clean(st.expiry_years),
        legs=[
            # clean() is defense-in-depth (fix round 1): build_structures'
            # _usable already refuses non-finite strikes/prices, but a NaN
            # must never reach the JSON layer even if a future path slips.
            OptionLegOut(
                action=leg.action, strike=clean(leg.strike), right=leg.right, price=clean(leg.price)
            )
            for leg in st.legs
        ],
        contracts=clean(contracts),
        net_premium_per_contract=clean(st.net_premium_per_contract),
        cost_annual=cost_annual,
        es_before=es_before_aligned,
        es_after=es_after,
        protection=clean(protection),
        protection_per_cost=clean(ppc),
        delta_es_ci_low=clean(ci[0]) if ci is not None else None,
        delta_es_ci_high=clean(ci[1]) if ci is not None else None,
        tail_n_days=tail.n_days if tail is not None else None,
        tail_mean_book=clean(tail.mean_book) if tail is not None else None,
        tail_mean_hedged=clean(tail.mean_hedged) if tail is not None else None,
    )
