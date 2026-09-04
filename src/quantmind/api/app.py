"""FastAPI app (Phase 1). Routes are thin wrappers over the tested pure core.

Serialization policy (Phase Plan): all timestamps UTC ISO Z-suffixed; NaN/Inf
map to null; empty cache returns structured empties, never 500. Security:
static bearer token (when configured) + localhost Host and browser-origin
checks — in place from Phase 1, not deferred to the execution phase.
"""

from __future__ import annotations

import math
import secrets

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from quantmind.brief import build_brief
from quantmind.datastore.store import BarStore
from quantmind.models.base import FitResult
from quantmind.models.registry import get_model, list_model_schemas
from quantmind.risk.returns import InsufficientDataError, simple_returns


def _clean(x: float | None) -> float | None:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return float(x)


def _json_safe_validation_detail(value):
    """Make malformed request evidence safe for strict JSON responses."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe_validation_detail(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_validation_detail(item) for item in value]
    if isinstance(value, BaseException):
        return str(value)
    return value


class Tile(BaseModel):
    symbol: str
    last_close: float
    change_1d: float


class Correlation(BaseModel):
    symbols: list[str]
    matrix: list[list[float | None]]


class BriefResponse(BaseModel):
    tiles: list[Tile]
    correlation: Correlation | None
    benchmark_es: float | None
    as_of: str | None


class FitRequest(BaseModel):
    symbol: str
    years: int = Field(5, ge=0, le=25)


class FitResponse(BaseModel):
    model_name: str
    params: dict[str, float]
    cis: dict[str, tuple[float, float]]
    diagnostics: dict[str, float]
    n_obs: int


class SimulateRequest(BaseModel):
    fit: FitResponse
    # Bounds are the resource-exhaustion guard: 200k paths x 2520 days is the
    # ceiling a request may allocate (security review).
    horizon: int = Field(126, ge=1, le=2520)
    n_paths: int = Field(10_000, ge=1, le=200_000)
    seed: int | None = None
    x0: float | None = None


class SimulateResponse(BaseModel):
    bands: dict[str, list[float]]
    sample_paths: list[list[float]]
    horizon: int
    n_paths: int


_DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
)


def create_app(
    store: BarStore,
    benchmark: str,
    api_token: str = "",
    broker=None,
    allowed_origins: tuple[str, ...] | None = None,
) -> FastAPI:
    app = FastAPI(title="QuantMind API", version="0.4.0.0")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, error: RequestValidationError):
        # Pydantic correctly rejects NaN/Inf, but its raw validation evidence can
        # itself contain those values. JSONResponse must never turn a client 422
        # into a server 500 while serializing that evidence.
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_validation_detail(error.errors())},
        )
    # Shared state for domain routers (routers/*.py): read via request.app.state
    app.state.store = store
    app.state.benchmark = benchmark
    app.state.broker = broker
    app.state.broker_connection_status = "connected" if broker is not None else "unavailable"
    app.state.broker_connection_error = None
    app.state.broker_mode = None
    app.state.broker_account_id = None
    accepted_origins = (
        _DEFAULT_ALLOWED_ORIGINS
        if allowed_origins is None
        else frozenset(allowed_origins)
    )

    async def auth(request: Request):
        # Security review: strict local Host plus browser request provenance,
        # followed by constant-time bearer comparison. Origin-less CLI/native
        # clients remain supported and rely on the bearer when configured.
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(403, "local access only")

        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
            raise HTTPException(403, "cross-site browser request rejected")

        origin = request.headers.get("origin")
        if origin is not None and origin not in accepted_origins:
            raise HTTPException(403, "browser origin not allowed")

        if api_token:
            header = request.headers.get("authorization", "")
            if not secrets.compare_digest(header, f"Bearer {api_token}"):
                raise HTTPException(401, "invalid or missing token")

    dep = [Depends(auth)]

    @app.get("/api/health", dependencies=dep)
    def health():
        return {"status": "ok"}

    @app.get("/api/brief", response_model=BriefResponse, dependencies=dep)
    def brief():
        b = build_brief(store, benchmark=benchmark)
        corr = None
        if b.correlation is not None:
            symbols = sorted(b.correlation.columns)
            m = b.correlation.loc[symbols, symbols]
            corr = Correlation(
                symbols=symbols,
                matrix=[[_clean(v) for v in row] for row in m.to_numpy().tolist()],
            )
        return BriefResponse(
            tiles=[Tile(symbol=t.symbol, last_close=t.last_close, change_1d=t.change_1d) for t in b.tiles],
            correlation=corr,
            benchmark_es=_clean(b.benchmark_es),
            as_of=None if b.as_of is None else b.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    @app.get("/api/models", dependencies=dep)
    def models():
        return list_model_schemas()

    def _series_for(symbol: str, years: int) -> pd.Series:
        symbol_map = store.read_symbol_map()
        if symbol in symbol_map:
            try:
                bars, _ = store.read_bars(con_id=symbol_map[symbol], bar_size="1d")
            except (FileNotFoundError, KeyError, OSError, ValueError):
                raise HTTPException(422, detail=f"symbol {symbol!r} mapped but has no cached bars")
            series = bars["close"]
        else:
            # named series (US10Y, NET_LIQUIDITY, ...) — the Lab's natural data sources
            try:
                series = store.read_series(symbol)
            except (FileNotFoundError, KeyError, OSError, ValueError):
                known = sorted(symbol_map) + store.list_series()
                raise HTTPException(422, detail=f"{symbol!r} not in cache; known: {known}")
        if years > 0:
            series = series.iloc[-(years * 252):]
        return series

    @app.post("/api/models/{name}/fit", response_model=FitResponse, dependencies=dep)
    def fit(name: str, req: FitRequest):
        try:
            model = get_model(name)
        except KeyError as e:
            raise HTTPException(404, detail=str(e))
        series = _series_for(req.symbol, req.years)
        if len(series) < 30:
            raise HTTPException(422, detail=f"only {len(series)} observations; need >= 30")
        try:
            fit_result = model.fit(series)
        except (InsufficientDataError, ValueError) as e:
            raise HTTPException(422, detail=str(e))
        return FitResponse(
            model_name=fit_result.model_name,
            params={k: _clean(v) for k, v in fit_result.params.items()},
            cis=fit_result.cis,
            diagnostics={k: _clean(v) for k, v in fit_result.diagnostics.items()},
            n_obs=fit_result.n_obs,
        )

    @app.post("/api/models/{name}/simulate", response_model=SimulateResponse, dependencies=dep)
    def simulate(name: str, req: SimulateRequest):
        try:
            model = get_model(name)
        except KeyError as e:
            raise HTTPException(404, detail=str(e))
        fit_result = FitResult(
            model_name=req.fit.model_name,
            params=req.fit.params,
            cis=req.fit.cis,
            diagnostics=req.fit.diagnostics,
            n_obs=req.fit.n_obs,
        )
        paths = model.simulate(
            fit_result, horizon=req.horizon, n_paths=req.n_paths, seed=req.seed, x0=req.x0
        )
        percentiles = {p: np.percentile(paths, q, axis=0) for p, q in
                       [("p5", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95)]}
        return SimulateResponse(
            bands={k: [float(x) for x in v] for k, v in percentiles.items()},
            sample_paths=paths[: min(100, len(paths))].tolist(),
            horizon=req.horizon,
            n_paths=req.n_paths,
        )

    from quantmind.api.routers import ROUTERS

    for r in ROUTERS:
        app.include_router(r, prefix="/api", dependencies=dep)

    return app
