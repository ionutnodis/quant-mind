"""book domain routes — named immutable book snapshots (wave-3 Task A1, the
book-flow spine). A `BookSnapshot` (quantmind.core.snapshot) pins EITHER the
live broker's current `Portfolio` OR a posted list of `PositionIn` legs at a
point in time; downstream routers (whatif.py, hedge.py) accept `book_ref:
snapshot_id` as an alternative to inline positions, so a page can "load
current book" once (BookBuilder.tsx) and every subsequent analysis
references the same frozen book.

Persistence: plain JSON files under `{store.root}/books/{snapshot_id}.json`
— a small self-contained helper (`write_book`/`read_book` below), NOT
`datastore/store.py` (A2 owns that file this wave; store.py's own
tmp-then-atomic-replace convention is reproduced here rather than imported,
since this isn't a Parquet write and doesn't belong on BarStore). Snapshot
ids are content hashes (`BookSnapshot.create`), so re-pinning the identical
book at the identical valuation_ts is just a no-op rewrite of the same file
— never a race, never a partial write.

`GET /api/book/current` reads the live broker's book and auto-pins it (wave-3
plan: "live broker read + auto-pin") so a single request both answers "what's
in the book right now" and hands back a `book_ref` usable immediately by
whatif/hedge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import PositionIn
from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio, Position

router = APIRouter()


class BookPinRequest(BaseModel):
    # None -> pin the live broker's current book (or an empty book if no
    # broker is configured). A posted list pins that instead — the "build a
    # hypothetical book, then pin it" path.
    positions: list[PositionIn] | None = None


class BookPositionOut(BaseModel):
    symbol: str
    qty: float
    con_id: int | None
    sec_type: str
    multiplier: float


class BookSnapshotOut(BaseModel):
    snapshot_id: str
    valuation_ts: str
    base_currency: str
    positions: list[BookPositionOut]


def _books_dir(store) -> Path:
    d = Path(store.root) / "books"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _leg_multiplier(p: PositionIn) -> float:
    """See _shared.py's PositionIn docstring: multiplier has no baked-in
    Pydantic default so a bare equity/ETF leg never silently gets 100x'd.
    An option leg (right set) defaults to the standard 100 when the caller
    didn't specify one; anything else defaults to 1.0 (a plain share)."""
    if p.multiplier is not None:
        return p.multiplier
    return 100.0 if p.right is not None else 1.0


def write_book(store, snapshot: BookSnapshot) -> None:
    """Persist an immutable snapshot as JSON (atomic tmp-then-replace, the
    same convention BarStore uses for its own writes — reproduced here as a
    small helper rather than imported, since store.py is A2's file this wave
    and this isn't a Parquet write)."""
    path = _books_dir(store) / f"{snapshot.snapshot_id}.json"
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "valuation_ts": snapshot.valuation_ts,
        "base_currency": snapshot.base_currency,
        "positions": [
            {
                "con_id": p.con_id,
                "symbol": p.symbol,
                "qty": p.qty,
                "sec_type": p.sec_type,
                "multiplier": p.multiplier,
            }
            for p in snapshot.portfolio.positions
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def read_book(store, snapshot_id: str) -> dict:
    """Raw persisted payload for `snapshot_id`; 422 naming the ref if it was
    never pinned (matches whatif.py/hedge.py's unknown-symbol 422 policy —
    an unresolvable book_ref is a client error, never a 500)."""
    path = _books_dir(store) / f"{snapshot_id}.json"
    if not path.exists():
        raise HTTPException(422, detail=f"unknown book_ref {snapshot_id!r}")
    return json.loads(path.read_text())


def read_book_positions(store, snapshot_id: str) -> list[PositionIn]:
    """Resolve a book_ref into the PositionIn legs whatif.py/hedge.py price
    off (symbol + qty is all those routers need; con_id/sec_type/multiplier
    stay in the persisted snapshot for GET /api/book/{id} display)."""
    payload = read_book(store, snapshot_id)
    return [PositionIn(symbol=p["symbol"], qty=p["qty"]) for p in payload["positions"]]


def _snapshot_out(payload: dict) -> BookSnapshotOut:
    return BookSnapshotOut(
        snapshot_id=payload["snapshot_id"],
        valuation_ts=payload["valuation_ts"],
        base_currency=payload["base_currency"],
        positions=[BookPositionOut(**p) for p in payload["positions"]],
    )


async def _live_portfolio(request: Request, valuation_ts: str) -> Portfolio:
    broker = request.app.state.broker
    if broker is None:
        return Portfolio(positions=(), as_of=valuation_ts)
    return await broker.get_portfolio()


def _portfolio_from_positions(store, positions: list[PositionIn], valuation_ts: str) -> Portfolio:
    symbol_map = store.read_symbol_map()
    unknown = sorted({p.symbol for p in positions} - symbol_map.keys())
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")
    return Portfolio(
        positions=tuple(
            Position(
                con_id=symbol_map[p.symbol],
                symbol=p.symbol,
                qty=p.qty,
                sec_type="OPT" if p.right is not None else "STK",
                multiplier=_leg_multiplier(p),
            )
            for p in positions
        ),
        as_of=valuation_ts,
    )


def _pin_and_respond(store, portfolio: Portfolio, valuation_ts: str) -> BookSnapshotOut:
    snapshot = BookSnapshot.create(portfolio, valuation_ts=valuation_ts, base_currency="USD")
    write_book(store, snapshot)
    return _snapshot_out(read_book(store, snapshot.snapshot_id))


@router.post("/book/pin", response_model=BookSnapshotOut)
async def pin_book(request: Request, req: BookPinRequest) -> BookSnapshotOut:
    store = request.app.state.store
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if req.positions is not None:
        portfolio = _portfolio_from_positions(store, req.positions, valuation_ts)
    else:
        portfolio = await _live_portfolio(request, valuation_ts)

    return _pin_and_respond(store, portfolio, valuation_ts)


@router.get("/book/current", response_model=BookSnapshotOut)
async def get_current_book(request: Request) -> BookSnapshotOut:
    store = request.app.state.store
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    portfolio = await _live_portfolio(request, valuation_ts)
    return _pin_and_respond(store, portfolio, valuation_ts)  # auto-pin (wave-3 plan)


@router.get("/book/{snapshot_id}", response_model=BookSnapshotOut)
def get_book(snapshot_id: str, request: Request) -> BookSnapshotOut:
    store = request.app.state.store
    return _snapshot_out(read_book(store, snapshot_id))
