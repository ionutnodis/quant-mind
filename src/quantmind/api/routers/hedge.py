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
`(objective.value - book_beta) * book_gross`, so
`hedge_qty = (objective.value - book_beta) * book_gross / (beta_cand * price_cand)`
`= -(book_beta - objective.value) * book_gross / (beta_cand * price_cand)`.
A signed-net book value is not the denominator of the book return/beta
calculation and therefore cannot size its beta overlay.
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
from dataclasses import dataclass
from itertools import islice
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.analytics.correlation import rolling_correlation
from quantmind.api.routers._shared import (
    FxEvidenceOut,
    PositionIn,
    clean,
    collect_currency_assertions,
    complete_fx_evidence,
    iso,
    load_base_currency_series,
    read_instrument_metadata_map,
    read_close_series,
    refuse_unsupported_contract_legs,
    weighted_portfolio_returns,
)
from quantmind.api.routers.book import (
    read_book,
    read_book_positions,
    validate_pinned_book_scope,
)
from quantmind.hedge.core import diversification_ratio, leverage_headroom, max_drawdown
from quantmind.fx import FxConversionUnavailable, FxConverter
from quantmind.risk.returns import InsufficientDataError, historical_es, rolling_beta

router = APIRouter()

_BETA_WINDOW = 60
_MIN_BETA_ABS = 0.1
_MAX_CANDIDATES_OUT = 20
_MAX_CANDIDATES_IN = 50
_MAX_DEFAULT_CANDIDATE_SCAN = 200
_MAX_BOOK_POSITIONS = 50
_MAX_CANDIDATE_MARK_AGE_BUSINESS_DAYS = 3
# At 97.5% confidence this leaves at least five observations in the
# historical-ES tail. A merely beta-estimable 60-day listing would otherwise
# rank on one worst day and could collapse the comparison window for every
# other candidate.
_MIN_PROTECTION_OBSERVATIONS = 200


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
    candidates: list[str] | None = Field(
        None, min_length=1, max_length=_MAX_CANDIDATES_IN
    )
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
    fx: FxEvidenceOut
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


class HedgeCandidateSkipOut(BaseModel):
    symbol: str
    reason: str


class HedgeResponse(BaseModel):
    fx: FxEvidenceOut
    benchmark: str
    objective: Objective
    book_value: float | None
    book_beta: float | None
    es_before: float | None
    # Additive evidence for the one common sample used by every candidate's
    # es_before/es_after/protection fields. The top-level es_before remains the
    # full available book baseline for backwards compatibility.
    comparison_as_of: str | None = None
    comparison_n_obs: int = 0
    n_candidates_evaluated: int
    candidates: list[HedgeCandidateOut]
    skipped_candidates: list[HedgeCandidateSkipOut]
    as_of: str | None


@dataclass(frozen=True)
class _PreparedCandidate:
    symbol: str
    prices: pd.Series
    returns: pd.Series
    beta: float | None
    unusable: bool
    corr_stability: float | None


def _portfolio_returns(
    series_map: dict[str, pd.Series], qtys: dict[str, float], symbols: list[str]
) -> tuple[pd.Series | None, dict[str, float], float, float, pd.DataFrame]:
    """Price-level inner join across `symbols`, then pct_change, weighted by
    |market value|-signed weight (mirrors routers/whatif.py's alignment).
    Returns (portfolio_returns, weights, book_value, gross, prices); `gross`
    is the denominator the caller must reuse for any per-book-dollar overlay
    computation (see module docstring's normalization convention)."""
    prices = pd.concat(
        {s: series_map[s] for s in symbols}, axis=1, sort=False
    ).dropna()
    if prices.empty:
        return None, {}, 0.0, 0.0, prices

    # Valuation, gross weights, returns, and response as_of must describe the
    # same book observation. Using each leg's independent last row mixes dates
    # whenever exchange calendars or cache freshness differ.
    last_close = {s: float(prices.iloc[-1][s]) for s in symbols}
    market_values = {s: qtys[s] * last_close[s] for s in symbols}
    gross = sum(abs(v) for v in market_values.values())
    weights = {s: (market_values[s] / gross if gross else 0.0) for s in symbols}
    book_value = sum(market_values.values())

    returns = prices.pct_change().dropna()
    if len(returns) == 0:
        return None, weights, book_value, gross, prices
    weights_arr = np.array([weights[s] for s in symbols])
    portfolio_returns = weighted_portfolio_returns(returns, symbols, weights_arr)
    return portfolio_returns, weights, book_value, gross, prices


def _business_day_age(older: pd.Timestamp, newer: pd.Timestamp) -> int:
    """Weekday-aware age between two already ordered market observations."""

    older_date = pd.Timestamp(older).date()
    newer_date = pd.Timestamp(newer).date()
    if older_date >= newer_date:
        return 0
    return int(
        np.busday_count(older_date.isoformat(), newer_date.isoformat())
    )


@router.post("/hedge", response_model=HedgeResponse)
def hedge(request: Request, req: HedgeRequest) -> HedgeResponse:
    store = request.app.state.store
    benchmark = request.app.state.benchmark
    base_currency = request.app.state.base_currency
    symbol_map = store.read_symbol_map()

    # book_ref resolves to the same PositionIn shape as an inline book
    # (read_book_positions 422s naming the ref if it's unknown); Field's
    # min_length/max_length=1..50 only runs on an inline `book` body, so a
    # book_ref-resolved list gets the same bounds check by hand here.
    if req.book is not None:
        book_positions = req.book
    else:
        pinned = read_book(store, req.book_ref)
        validate_pinned_book_scope(request.app.state, pinned)
        book_positions = read_book_positions(store, req.book_ref)
    if not book_positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(book_positions) > _MAX_BOOK_POSITIONS:
        raise HTTPException(422, detail=f"book has {len(book_positions)} positions; max {_MAX_BOOK_POSITIONS}")
    refuse_unsupported_contract_legs(book_positions, route_name="Hedge")

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

    asserted_currencies = collect_currency_assertions(book_positions)
    series_map, book_currencies, fx_converter = load_base_currency_series(
        store,
        symbol_map,
        [*unique_book, benchmark],
        years=req.years,
        base_currency=base_currency,
        asserted_currencies=asserted_currencies,
    )
    fx_sources = (
        {fx_converter.source} if fx_converter.source != "identity" else set()
    )
    fx_as_of_dates = [fx_converter.as_of] if fx_converter.as_of is not None else []
    fx_fetched_at_values = (
        [fx_converter.fetched_at] if fx_converter.fetched_at else []
    )

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
        requested = sorted(dict.fromkeys(req.candidates))
        overlaps = [symbol for symbol in requested if symbol in unique_book]
        if overlaps:
            detail = "; ".join(
                f"{symbol}: candidate is already present in book"
                for symbol in overlaps
            )
            raise HTTPException(
                422,
                detail=f"requested hedge candidates unavailable: {detail}",
            )
        candidate_pool = requested
        default_scan_omitted = 0
    else:
        book_symbols = set(unique_book)
        candidate_count = len(symbol_map) - len(book_symbols)
        candidate_pool = list(
            islice(
                (symbol for symbol in symbol_map if symbol not in book_symbols),
                _MAX_DEFAULT_CANDIDATE_SCAN,
            )
        )
        default_scan_omitted = max(0, candidate_count - len(candidate_pool))

    if not candidate_pool:
        raise HTTPException(
            422,
            detail="no usable candidates: candidate universe is empty after excluding book symbols",
        )

    prepared_candidates: list[_PreparedCandidate] = []
    skipped_candidates: list[HedgeCandidateSkipOut] = []
    candidate_metadata = read_instrument_metadata_map(store)
    converter_cache: dict[str, FxConverter | None] = {
        currency: fx_converter for currency in set(book_currencies.values())
    }
    book_as_of = book_prices.index[-1]
    comparison_index = book_returns.index
    for candidate_index, csym in enumerate(candidate_pool):
        if (
            req.candidates is None
            and len(prepared_candidates) >= _MAX_CANDIDATES_IN
        ):
            skipped_candidates.extend(
                HedgeCandidateSkipOut(
                    symbol=remaining_symbol,
                    reason=(
                        "not inspected because the default candidate evaluation "
                        f"limit ({_MAX_CANDIDATES_IN}) was reached"
                    ),
                )
                for remaining_symbol in candidate_pool[candidate_index:]
            )
            break
        currency_value = (candidate_metadata.get(csym) or {}).get("currency")
        currency = (
            str(currency_value).strip().upper() if currency_value else None
        )
        if currency is None:
            skipped_candidates.append(
                HedgeCandidateSkipOut(symbol=csym, reason="missing currency metadata")
            )
            continue
        try:
            if currency not in converter_cache:
                try:
                    converter_cache[currency] = FxConverter.from_store(
                        store,
                        base_currency=base_currency,
                        currencies={currency},
                    )
                except (FxConversionUnavailable, ValueError):
                    converter_cache[currency] = None
            candidate_converter = converter_cache[currency]
            if candidate_converter is None:
                skipped_candidates.append(
                    HedgeCandidateSkipOut(
                        symbol=csym,
                        reason=f"dated {currency} FX evidence is unavailable",
                    )
                )
                continue
            local_prices = read_close_series(
                store,
                symbol_map[csym],
                csym,
                req.years,
            )
            cand_prices = candidate_converter.convert_series(local_prices, currency)
        except HTTPException:
            skipped_candidates.append(
                HedgeCandidateSkipOut(symbol=csym, reason="cached daily bars are unavailable")
            )
            continue
        except FxConversionUnavailable:
            skipped_candidates.append(
                HedgeCandidateSkipOut(
                    symbol=csym,
                    reason=f"dated {currency} FX evidence does not cover the price history",
                )
            )
            continue
        cand_prices = cand_prices.sort_index()
        cand_prices = cand_prices.loc[cand_prices.index <= book_as_of]
        if cand_prices.empty:
            skipped_candidates.append(
                HedgeCandidateSkipOut(
                    symbol=csym,
                    reason=(
                        "no cached daily bars on or before the aligned book "
                        "valuation date"
                    ),
                )
            )
            continue
        candidate_as_of = pd.Timestamp(cand_prices.index[-1])
        mark_age = _business_day_age(candidate_as_of, pd.Timestamp(book_as_of))
        if mark_age > _MAX_CANDIDATE_MARK_AGE_BUSINESS_DAYS:
            skipped_candidates.append(
                HedgeCandidateSkipOut(
                    symbol=csym,
                    reason=(
                        f"candidate mark from {candidate_as_of.date()} is stale "
                        f"relative to book as-of {pd.Timestamp(book_as_of).date()} "
                        f"({mark_age} business days; max "
                        f"{_MAX_CANDIDATE_MARK_AGE_BUSINESS_DAYS})"
                    ),
                )
            )
            continue
        if candidate_converter.source != "identity":
            fx_sources.add(candidate_converter.source)
        if candidate_converter.as_of is not None:
            fx_as_of_dates.append(candidate_converter.as_of)
        if candidate_converter.fetched_at:
            fx_fetched_at_values.append(candidate_converter.fetched_at)

        cand_returns = cand_prices.pct_change().dropna()

        aligned_c = pd.concat(
            {"asset": cand_returns, "bench": bench_returns},
            axis=1,
            sort=False,
        ).dropna()
        beta_cand: float | None = None
        if len(aligned_c) >= _BETA_WINDOW + 2:
            try:
                cb_series = rolling_beta(aligned_c["asset"], aligned_c["bench"], window=_BETA_WINDOW, rf=0.0)
                cb_valid = cb_series.dropna()
                if len(cb_valid):
                    beta_cand = float(cb_valid.iloc[-1])
            except InsufficientDataError:
                beta_cand = None

        aligned_bc = pd.concat(
            {"book": book_returns, "cand": cand_returns},
            axis=1,
            sort=False,
        ).dropna()
        corr_stability: float | None = None
        if len(aligned_bc) >= _BETA_WINDOW + 2:
            roll_corr = rolling_correlation(aligned_bc["book"], aligned_bc["cand"], window=_BETA_WINDOW).dropna()
            if len(roll_corr):
                corr_stability = clean(float(roll_corr.std()))

        unusable = beta_cand is None or not math.isfinite(beta_cand) or abs(beta_cand) < _MIN_BETA_ABS

        if not unusable and book_beta is not None:
            candidate_comparison_index = book_returns.index.intersection(
                cand_returns.index, sort=False
            ).sort_values()
            if len(candidate_comparison_index) < _MIN_PROTECTION_OBSERVATIONS:
                skipped_candidates.append(
                    HedgeCandidateSkipOut(
                        symbol=csym,
                        reason=(
                            "insufficient common comparison history: "
                            f"{len(candidate_comparison_index)} observations; need "
                            f"at least {_MIN_PROTECTION_OBSERVATIONS}"
                        ),
                    )
                )
                continue

        prepared_candidates.append(
            _PreparedCandidate(
                symbol=csym,
                prices=cand_prices,
                returns=cand_returns,
                beta=beta_cand,
                unusable=unusable,
                corr_stability=corr_stability,
            )
        )

    comparable_candidates = [
        candidate
        for candidate in prepared_candidates
        if not candidate.unusable and book_beta is not None
    ]
    if req.candidates is not None and comparable_candidates:
        for candidate in comparable_candidates:
            comparison_index = comparison_index.intersection(
                candidate.returns.index, sort=False
            ).sort_values()
        if len(comparison_index) < _MIN_PROTECTION_OBSERVATIONS:
            names = ", ".join(
                sorted(candidate.symbol for candidate in comparable_candidates)
            )
            raise HTTPException(
                422,
                detail=(
                    "requested hedge candidates unavailable: "
                    f"{names} collectively share {len(comparison_index)} common "
                    "comparison observations; need at least "
                    f"{_MIN_PROTECTION_OBSERVATIONS}"
                ),
            )
        has_comparable_candidate = True
    elif comparable_candidates:
        accepted_symbols: set[str] = set()
        ordered_candidates = sorted(
            comparable_candidates,
            key=lambda candidate: (
                -len(book_returns.index.intersection(candidate.returns.index)),
                candidate.symbol,
            ),
        )
        for candidate in ordered_candidates:
            cohort_index = comparison_index.intersection(
                candidate.returns.index, sort=False
            ).sort_values()
            if len(cohort_index) < _MIN_PROTECTION_OBSERVATIONS:
                skipped_candidates.append(
                    HedgeCandidateSkipOut(
                        symbol=candidate.symbol,
                        reason=(
                            "incompatible with the common comparison cohort: "
                            f"{len(cohort_index)} observations; need at least "
                            f"{_MIN_PROTECTION_OBSERVATIONS}"
                        ),
                    )
                )
                continue
            comparison_index = cohort_index
            accepted_symbols.add(candidate.symbol)
        prepared_candidates = [
            candidate
            for candidate in prepared_candidates
            if candidate.unusable or candidate.symbol in accepted_symbols
        ]
        has_comparable_candidate = bool(accepted_symbols)
    else:
        has_comparable_candidate = False

    # Protection is a cross-candidate ranking metric, so every beta-usable
    # candidate must use one common book/candidate sample. Candidate-specific
    # overlaps make shorter or differently listed histories incomparable.
    comparison_book_returns = book_returns.loc[comparison_index]
    if has_comparable_candidate:
        try:
            comparison_es_before = clean(
                historical_es(comparison_book_returns, confidence=0.975)
            )
        except InsufficientDataError:
            comparison_es_before = None
    else:
        comparison_es_before = None

    results: list[HedgeCandidateOut] = []
    for candidate in prepared_candidates:
        hedge_qty = hedge_notional = es_after = protection = residual_beta = None
        beta_cand = candidate.beta

        if not candidate.unusable and book_beta is not None and beta_cand is not None:
            price_cand_last = float(candidate.prices.iloc[-1])
            if math.isfinite(price_cand_last) and price_cand_last != 0:
                raw_size = (
                    (book_beta - req.objective.value)
                    * book_gross
                    / (beta_cand * price_cand_last)
                )
                hedge_qty = -raw_size
                hedge_notional = hedge_qty * price_cand_last

                # Overlay, not a re-blended portfolio (see module docstring's
                # normalization convention). The common index guarantees
                # es_before and every ranked es_after use identical dates.
                candidate_returns = candidate.returns.loc[comparison_index]
                hedged_returns: pd.Series | None = None
                if len(comparison_index) > 0 and book_gross:
                    hedge_leg_returns = (
                        hedge_notional * candidate_returns / book_gross
                    )
                    hedged_returns = comparison_book_returns + hedge_leg_returns

                if hedged_returns is not None:
                    try:
                        es_after = clean(
                            historical_es(hedged_returns, confidence=0.975)
                        )
                    except InsufficientDataError:
                        es_after = None
                    if comparison_es_before is not None and es_after is not None:
                        protection = comparison_es_before - es_after

                    aligned_h = pd.concat(
                        {"book": hedged_returns, "bench": bench_returns},
                        axis=1,
                        sort=False,
                    ).dropna()
                    if len(aligned_h) >= _BETA_WINDOW + 2:
                        try:
                            rb_series = rolling_beta(
                                aligned_h["book"],
                                aligned_h["bench"],
                                window=_BETA_WINDOW,
                                rf=0.0,
                            )
                            rb_valid = rb_series.dropna()
                            if len(rb_valid):
                                residual_beta = clean(float(rb_valid.iloc[-1]))
                        except InsufficientDataError:
                            residual_beta = None

        results.append(
            HedgeCandidateOut(
                symbol=candidate.symbol,
                beta=clean(beta_cand),
                unusable=candidate.unusable,
                hedge_qty=clean(hedge_qty),
                hedge_notional=clean(hedge_notional),
                es_before=comparison_es_before,
                es_after=es_after,
                protection=clean(protection),
                residual_beta=residual_beta,
                corr_stability=candidate.corr_stability,
            )
        )

    if default_scan_omitted:
        skipped_candidates.append(
            HedgeCandidateSkipOut(
                symbol="<remaining cached candidates>",
                reason=(
                    f"{default_scan_omitted} additional cached candidates were not "
                    "inspected because the default candidate scan limit "
                    f"({_MAX_DEFAULT_CANDIDATE_SCAN}) was reached"
                ),
            )
        )

    n_evaluated = len(results)
    if req.candidates is not None and skipped_candidates:
        details = "; ".join(
            f"{candidate.symbol}: {candidate.reason}"
            for candidate in skipped_candidates
        )
        raise HTTPException(
            422,
            detail=f"requested hedge candidates unavailable: {details}",
        )
    if n_evaluated == 0:
        details = "; ".join(
            f"{candidate.symbol}: {candidate.reason}"
            for candidate in skipped_candidates
        )
        raise HTTPException(
            422,
            detail=(
                "no usable candidates: none of the candidate symbols had sufficient "
                f"cached data{f' ({details})' if details else ''}"
            ),
        )

    # Rank by protection descending (Engineering Constraint 12: cointegration
    # is diagnostic only, never the ranking key); unusable/None-protection
    # candidates sort last but are still returned, flagged.
    results.sort(key=lambda r: (r.protection is None, -(r.protection if r.protection is not None else 0.0)))

    hedge_fx = complete_fx_evidence(fx_converter, base_currency=base_currency)
    if fx_sources:
        hedge_fx = hedge_fx.model_copy(
            update={
                "status": "converted",
                "source": ", ".join(sorted(fx_sources)),
                "as_of": min(fx_as_of_dates) if fx_as_of_dates else None,
                "fetched_at": (
                    min(fx_fetched_at_values) if fx_fetched_at_values else None
                ),
                "note": (
                    f"Book and hedge candidates are normalized to {base_currency} "
                    "with dated FX evidence."
                ),
            }
        )
    return HedgeResponse(
        fx=hedge_fx,
        benchmark=benchmark,
        objective=req.objective,
        book_value=clean(book_value),
        book_beta=book_beta,
        es_before=es_before,
        comparison_as_of=(
            iso(comparison_index[-1]) if has_comparable_candidate else None
        ),
        comparison_n_obs=(
            len(comparison_index) if has_comparable_candidate else 0
        ),
        n_candidates_evaluated=n_evaluated,
        candidates=results[:_MAX_CANDIDATES_OUT],
        skipped_candidates=skipped_candidates,
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
    base_currency = request.app.state.base_currency

    if req.book is not None:
        book_positions = req.book
    else:
        pinned = read_book(store, req.book_ref)
        validate_pinned_book_scope(request.app.state, pinned)
        book_positions = read_book_positions(store, req.book_ref)
    if not book_positions:
        raise HTTPException(422, detail="book_ref resolved to an empty book")
    if len(book_positions) > _MAX_BOOK_POSITIONS:
        raise HTTPException(422, detail=f"book has {len(book_positions)} positions; max {_MAX_BOOK_POSITIONS}")
    refuse_unsupported_contract_legs(book_positions, route_name="Leverage")

    unique_book = list(dict.fromkeys(p.symbol for p in book_positions))
    qtys: dict[str, float] = {}
    for p in book_positions:
        qtys[p.symbol] = qtys.get(p.symbol, 0.0) + p.qty

    unknown = sorted(s for s in unique_book if s not in symbol_map)
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")

    asserted_currencies = collect_currency_assertions(book_positions)
    series_map, _currencies, fx_converter = load_base_currency_series(
        store,
        symbol_map,
        unique_book,
        years=req.years,
        base_currency=base_currency,
        asserted_currencies=asserted_currencies,
    )
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
        fx=complete_fx_evidence(fx_converter, base_currency=base_currency),
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
