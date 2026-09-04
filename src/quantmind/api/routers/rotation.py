"""Rotation domain routes (wave-3B Today task): POST /api/rotation.

Turns Today's correlation heatmap into a "where is the money flowing?"
instrument: pick a universe (sectors/factors/world/custom), a correlation
window, and a return lookback, and get back a clustered correlation matrix +
per-symbol returns + (with `anchor` set) an "other side of the trade"
ranking — the universe scored by (negative correlation to the anchor) x
(positive recent return), so clicking a symbol that's down surfaces what's
both uncorrelated-or-inverse AND actually moving up right now.

Store-only (never a live broker call, Global Constraints): reads cached
daily bars via `store.read_bars`/`store.read_symbol_map`. A symbol not in
the symbol map at all is a genuine client input error -> 422 ("unknown
symbol"); a symbol that IS mapped but has no cached bars yet (synced
universe member just never fetched) degrades gracefully into `missing`
(pattern: routers/macro.py's `_rotation_row` — "mapped symbol without bars
is skipped, not 500") rather than failing the whole request.

Clustered ordering is a small greedy nearest-neighbor chain implemented
locally (`cluster_order` below) rather than in `analytics/` (unowned this
wave) or via scipy (no new dependency for one heatmap ordering) — a
lightweight approximation of hierarchical clustering: start from the most
"central" symbol (highest average correlation to the rest of the universe),
then repeatedly append whichever remaining symbol correlates most with the
last one placed, so blocks of comovers land adjacent to each other on the
heatmap axes.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from quantmind.analytics.correlation import crisis_correlation
from quantmind.api.routers._shared import clean, iso
from quantmind.api.routers.macro import FACTORS, SECTORS
from quantmind.risk.returns import InsufficientDataError

router = APIRouter()

# World-ETF universe (mirrors sync_cli.py's WORLD_ETF_REGIONS keys). Kept as
# a local literal rather than importing sync_cli (that module's top-level
# `if __name__ == "__main__": asyncio.run(main(...))` guard makes the import
# itself safe, but pulling this router's dependency graph through the sync
# CLI's is unnecessary coupling for a 10-symbol list owned elsewhere) —
# duplication is the accepted tradeoff, flagged here so a universe change
# in sync_cli.py's WORLD_ETF_REGIONS is remembered to update this list too.
WORLD = ["EZU", "EWU", "EWY", "EWT", "INDA", "MCHI", "EWZ", "EEM", "EFA", "SH"]

_DEFAULT_UNIVERSES: dict[str, list[str]] = {"sectors": SECTORS, "factors": FACTORS, "world": WORLD}

_MAX_CUSTOM_SYMBOLS = 50


class RotationRequest(BaseModel):
    universe: Literal["sectors", "factors", "world", "custom"]
    # Override the default membership of a named universe, or (required)
    # supply the membership for "custom".
    symbols: list[str] | None = Field(None, max_length=_MAX_CUSTOM_SYMBOLS)
    corr_window: Literal[20, 60, 120] = 60
    return_days: int = Field(5, ge=1, le=21)
    # The instrument the user clicked (typically one that's down) — when
    # set, the response's `other_side` ranks the rest of the universe by
    # "money flowing away from it".
    anchor: str | None = None

    @model_validator(mode="after")
    def _custom_requires_symbols(self) -> "RotationRequest":
        if self.universe == "custom" and not self.symbols:
            raise ValueError("universe 'custom' requires a non-empty `symbols` list")
        if self.symbols is not None and len(self.symbols) == 0:
            raise ValueError("`symbols` must be non-empty when provided")
        return self


class SymbolReturnOut(BaseModel):
    symbol: str
    ret: float | None


class OtherSideOut(BaseModel):
    symbol: str
    corr: float | None
    ret: float | None
    score: float | None


class RotationResponse(BaseModel):
    universe: str
    symbols: list[str]  # clustered order
    matrix: list[list[float | None]]  # correlation matrix, rows/cols == symbols
    corr_window: int
    return_days: int
    returns: list[SymbolReturnOut]  # aligned to `symbols` (clustered) order
    anchor: str | None
    other_side: list[OtherSideOut] | None
    as_of: str | None
    missing: list[str]


def cluster_order(corr: pd.DataFrame) -> list[str]:
    """Greedy nearest-neighbor chain ordering over a correlation matrix
    (see module docstring). Deterministic given `corr`'s column order.
    NaN correlations (never fully overlapping series) are treated as -2.0
    for tie-breaking so an unaligned pair never "wins" a spot."""
    symbols = list(corr.columns)
    if len(symbols) <= 2:
        return symbols

    def score(a: str, b: str) -> float:
        v = corr.loc[a, b]
        return float(v) if pd.notna(v) else -2.0

    avg_corr = corr.apply(lambda col: np.nanmean(col.to_numpy()), axis=0)
    start = avg_corr.idxmax()
    order = [start]
    remaining = set(symbols)
    remaining.remove(start)
    while remaining:
        last = order[-1]
        best = max(remaining, key=lambda s: score(last, s))
        order.append(best)
        remaining.remove(best)
    return order


def _closes_for(
    store, symbol_map: dict[str, int], symbols: list[str], strict: bool
) -> tuple[dict[str, pd.Series], list[str]]:
    """Resolve each requested symbol to its cached close series.

    `strict` distinguishes client-supplied symbols (explicit `symbols` on
    the request — including "custom" universes, which always require it)
    from a named universe's own DEFAULT membership: an explicit unknown
    ticker is a genuine client input error -> 422 (`strict=True`), but a
    default sector/factor/world member that simply isn't cached yet is
    exactly macro.py's "mapped/unmapped symbol skipped, not 500" case
    (`strict=False`) — the user never typed that symbol, so it can't be
    their mistake.

    Either way, a symbol that IS mapped but has no cached bars (synced
    universe member never actually fetched) always degrades into
    `missing`, never a 500."""
    unknown = sorted({s for s in symbols if s not in symbol_map})
    if unknown and strict:
        raise HTTPException(422, detail=f"unknown symbol(s): {unknown}")

    closes: dict[str, pd.Series] = {}
    missing: list[str] = list(unknown)
    for symbol in symbols:
        if symbol not in symbol_map:
            continue  # already recorded in `missing` above
        try:
            bars, _ = store.read_bars(con_id=symbol_map[symbol], bar_size="1d")
        except (FileNotFoundError, KeyError, OSError, ValueError):
            missing.append(symbol)
            continue
        close = bars["close"]
        if close.empty:
            missing.append(symbol)
            continue
        closes[symbol] = close
    return closes, missing


def _symbol_return(close: pd.Series, return_days: int) -> float | None:
    if len(close) <= return_days:
        return None
    return clean(float(close.iloc[-1] / close.iloc[-1 - return_days] - 1.0))


def _other_side_score(corr: float | None, ret: float | None) -> float | None:
    """(negative corr) x (positive recent return), per the wave-3B "other
    side of the trade" spec — clipped so a symbol only scores above zero
    when BOTH legs point the intended direction: a symbol that's positively
    correlated with the (down) anchor and also falling (e.g. another leg of
    the same down-move) must NOT outrank a truly uncorrelated-and-rising
    symbol just because two negative signs cancel out."""
    if corr is None or ret is None:
        return None
    return clean(max(-corr, 0.0) * max(ret, 0.0))


def rank_other_side(rows: list[OtherSideOut]) -> list[OtherSideOut]:
    """Ordering for the "other side of the trade" list. Primary: score
    descending (the clipped negative-corr x positive-return product).
    Secondary (fix-round-1 adjudication): the clip zeroes out
    negatively-correlated-but-FLAT symbols — which is exactly the "money
    hasn't rotated there YET" candidate the user wants surfaced — so
    equal-score rows tie-break by corr ascending (most inverse first): a
    strongly-inverse-quiet name outranks an uncorrelated-quiet one instead
    of drowning in the zero-score noise. Null score/corr sink last."""
    return sorted(
        rows,
        key=lambda o: (
            o.score is None,
            -(o.score if o.score is not None else 0.0),
            o.corr is None,
            o.corr if o.corr is not None else 0.0,
        ),
    )


class CrisisRequest(BaseModel):
    universe: Literal["sectors", "factors", "world", "custom"]
    symbols: list[str] | None = Field(None, max_length=_MAX_CUSTOM_SYMBOLS)
    # Worst `tail` fraction of benchmark days define the crisis regime; deep
    # history (`years`) is needed so the tail has enough days to be meaningful.
    tail: float = Field(0.10, gt=0.0, lt=1.0)
    min_tail: int = Field(20, ge=2, le=250)
    years: int = Field(5, ge=1, le=25)

    @model_validator(mode="after")
    def _custom_requires_symbols(self) -> "CrisisRequest":
        if self.universe == "custom" and not self.symbols:
            raise ValueError("universe 'custom' requires a non-empty `symbols` list")
        if self.symbols is not None and len(self.symbols) == 0:
            raise ValueError("`symbols` must be non-empty when provided")
        return self


class CrisisResponse(BaseModel):
    universe: str
    symbols: list[str]  # clustered order (from the normal-regime matrix)
    normal_matrix: list[list[float | None]]
    crisis_matrix: list[list[float | None]]
    normal_mean_corr: float | None
    crisis_mean_corr: float | None
    crisis_mean_corr_ci: tuple[float | None, float | None]
    tail_n: int
    benchmark: str
    caveat: str
    as_of: str | None
    missing: list[str]


def _benchmark_returns(store, symbol_map: dict[str, int], benchmark: str, years: int) -> pd.Series | None:
    if benchmark not in symbol_map:
        return None
    try:
        bars, _ = store.read_bars(con_id=symbol_map[benchmark], bar_size="1d")
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None
    close = bars["close"]
    if years > 0:
        close = close.iloc[-(years * 252):]
    return close.pct_change().dropna()


@router.post("/rotation/crisis", response_model=CrisisResponse)
def rotation_crisis(request: Request, req: CrisisRequest) -> CrisisResponse:
    """Normal vs crisis (benchmark worst-day) correlation over a universe —
    the "diversification decays in a crisis" lens. Store-only, deep history."""
    store = request.app.state.store
    symbol_map = store.read_symbol_map()
    benchmark = request.app.state.benchmark

    universe_symbols = req.symbols if req.symbols is not None else _DEFAULT_UNIVERSES[req.universe]
    closes, missing = _closes_for(store, symbol_map, universe_symbols, strict=req.symbols is not None)
    symbols = sorted(closes)
    if len(symbols) < 2:
        raise HTTPException(422, detail="crisis correlation needs >= 2 cached instruments")

    def _windowed(s: pd.Series) -> pd.Series:
        return s.iloc[-(req.years * 252):] if req.years > 0 else s

    returns_df = pd.DataFrame({s: _windowed(closes[s]).pct_change().dropna() for s in symbols}).dropna()
    bench = _benchmark_returns(store, symbol_map, benchmark, req.years)
    if bench is None:
        raise HTTPException(422, detail=f"benchmark {benchmark!r} not cached")

    try:
        res = crisis_correlation(returns_df, bench, tail=req.tail, min_tail=req.min_tail, seed=0)
    except InsufficientDataError as e:
        raise HTTPException(422, detail=str(e))

    clustered = cluster_order(res.normal_corr)
    normal = res.normal_corr.loc[clustered, clustered]
    crisis = res.crisis_corr.loc[clustered, clustered]

    return CrisisResponse(
        universe=req.universe,
        symbols=clustered,
        normal_matrix=[[clean(v) for v in row] for row in normal.to_numpy().tolist()],
        crisis_matrix=[[clean(v) for v in row] for row in crisis.to_numpy().tolist()],
        normal_mean_corr=clean(res.normal_mean_corr),
        crisis_mean_corr=clean(res.crisis_mean_corr),
        crisis_mean_corr_ci=(clean(res.crisis_mean_corr_ci[0]), clean(res.crisis_mean_corr_ci[1])),
        tail_n=res.tail_n,
        benchmark=benchmark,
        caveat=res.caveat,
        as_of=iso(max(closes[s].index[-1] for s in symbols)),
        missing=missing,
    )


@router.post("/rotation", response_model=RotationResponse)
def rotation(request: Request, req: RotationRequest) -> RotationResponse:
    store = request.app.state.store
    symbol_map = store.read_symbol_map()

    universe_symbols = req.symbols if req.symbols is not None else _DEFAULT_UNIVERSES[req.universe]
    closes, missing = _closes_for(store, symbol_map, universe_symbols, strict=req.symbols is not None)

    if req.anchor is not None and req.anchor not in closes:
        raise HTTPException(422, detail=f"unknown or uncached anchor symbol: {req.anchor!r}")

    symbols = sorted(closes)  # stable base order before clustering
    if len(symbols) == 0:
        return RotationResponse(
            universe=req.universe, symbols=[], matrix=[], corr_window=req.corr_window,
            return_days=req.return_days, returns=[], anchor=req.anchor, other_side=None,
            as_of=None, missing=missing,
        )

    returns_df = pd.DataFrame({s: closes[s].pct_change().dropna() for s in symbols}).dropna()
    as_of_dates = [closes[s].index[-1] for s in symbols]
    as_of = iso(max(as_of_dates))

    if len(symbols) == 1 or returns_df.shape[0] < 2:
        clustered = symbols
        matrix = [[1.0 if i == j else None for j in range(len(symbols))] for i in range(len(symbols))]
    else:
        window = returns_df.tail(req.corr_window)
        corr = window.corr()
        clustered = cluster_order(corr)
        corr = corr.loc[clustered, clustered]
        matrix = [[clean(v) for v in row] for row in corr.to_numpy().tolist()]

    returns_out = [
        SymbolReturnOut(symbol=s, ret=_symbol_return(closes[s], req.return_days)) for s in clustered
    ]

    other_side: list[OtherSideOut] | None = None
    if req.anchor is not None and len(symbols) > 1 and returns_df.shape[0] >= 2:
        window = returns_df.tail(req.corr_window)
        corr_col = window.corr()[req.anchor]
        other_side_rows = []
        for s in clustered:
            if s == req.anchor:
                continue
            c = corr_col.get(s)
            c = float(c) if pd.notna(c) else None
            r = _symbol_return(closes[s], req.return_days)
            sc = _other_side_score(c, r)
            other_side_rows.append(OtherSideOut(symbol=s, corr=clean(c), ret=r, score=sc))
        other_side = rank_other_side(other_side_rows)

    return RotationResponse(
        universe=req.universe,
        symbols=clustered,
        matrix=matrix,
        corr_window=req.corr_window,
        return_days=req.return_days,
        returns=returns_out,
        anchor=req.anchor,
        other_side=other_side,
        as_of=as_of,
        missing=missing,
    )
