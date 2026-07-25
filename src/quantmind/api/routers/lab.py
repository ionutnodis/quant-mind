"""lab domain routes — the model-registry apply-to-book pipeline (Task 3, the
Lab bench centerpiece per DESIGN.md).

POST /api/lab/apply is the only route owned here. It deliberately does NOT
duplicate /api/models/{name}/fit or /simulate (quantmind/api/app.py) — the
frontend calls those directly for fit/simulate, and this endpoint reuses the
same FitResult -> model.simulate() path, then pipes the terminal factor draws
through quantmind.exposure.bridge.apply_to_book into a P&L distribution.
UnsupportedMappingError (wrong exposure units/kind) becomes a 422 with the
bridge's own "refusing" message — never a dimensionally wrong number.
"""

from __future__ import annotations

import math

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantmind.api.app import FitResponse
from quantmind.exposure.bridge import Exposure, UnsupportedMappingError, apply_to_book
from quantmind.models.base import FitResult
from quantmind.models.registry import get_model

router = APIRouter()


def _clean(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return float(x)


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


class Histogram(BaseModel):
    edges: list[float]
    counts: list[int]


class LabApplyResponse(BaseModel):
    histogram: Histogram
    mean: float | None
    p5: float | None
    p50: float | None
    p95: float | None
    es: float | None
    horizon: int
    n_paths: int


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
    initial = req.x0 if req.x0 is not None else req.fit.params.get("mu")
    if initial is None:
        raise HTTPException(422, detail="fit has no 'mu' parameter; provide x0 explicitly")

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

    n_bins = min(60, max(1, len(pnl)))
    counts, edges = np.histogram(pnl, bins=n_bins)
    p5, p50, p95 = (float(v) for v in np.percentile(pnl, [5, 50, 95]))

    return LabApplyResponse(
        histogram=Histogram(edges=[float(e) for e in edges], counts=[int(c) for c in counts]),
        mean=_clean(float(np.mean(pnl))),
        p5=_clean(p5),
        p50=_clean(p50),
        p95=_clean(p95),
        es=_clean(_tail_es(pnl)),
        horizon=req.horizon,
        n_paths=req.n_paths,
    )
