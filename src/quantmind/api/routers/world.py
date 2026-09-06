"""Portfolio-aware world feed. Cached reads and explicitly requested ingestion."""
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from quantmind.api.routers.book import read_book, validate_pinned_book_scope
from quantmind.world.models import WorldProfile
from quantmind.world.relevance import RankedEvent, book_symbols

router = APIRouter()


class WorldSourceStatus(BaseModel):
    id: str
    name: str
    category: str
    homepage: str
    access: str
    description: str
    enabled: bool
    state: Literal["never", "ok", "error", "disabled"]
    last_attempt: str | None
    last_success: str | None
    next_refresh: str | None
    item_count: int
    error: str | None
    stale: bool


class WorldContext(BaseModel):
    book_ref: str | None
    symbols: list[str]
    label: str


class WorldResponse(BaseModel):
    items: list[RankedEvent]
    sources: list[WorldSourceStatus]
    profile: WorldProfile
    context: WorldContext
    as_of: str | None
    refreshing: bool


class WorldRefreshResult(BaseModel):
    updated: int
    failed: int
    skipped: int


@router.get("/world", response_model=WorldResponse)
def world(request: Request, book_ref: str | None = Query(None, max_length=12)):
    symbols = []
    if book_ref is not None:
        pinned = read_book(request.app.state.store, book_ref)
        validate_pinned_book_scope(request.app.state, pinned)
        symbols = book_symbols(pinned["positions"])
    return request.app.state.world.snapshot(symbols, book_ref)


@router.put("/world/profile", response_model=WorldProfile)
def save_profile(profile: WorldProfile, request: Request):
    """One local investor lens; complete replacement, last write wins."""
    request.app.state.world.cache.save_profile(profile)
    return profile


@router.post("/world/refresh", response_model=WorldRefreshResult)
async def refresh(request: Request):
    """Single-flight, cadence-limited refresh. No user URLs or portfolio payload."""
    return await request.app.state.world.refresh()
