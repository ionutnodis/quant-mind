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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from quantmind.api.routers._shared import PositionIn, _validate_option_legs
from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio, Position

router = APIRouter()

# Snapshot ids are always a 12-hex-char sha256 prefix (BookSnapshot.create).
# book_ref is client-controlled and lands directly in a filesystem path
# (`{root}/books/{snapshot_id}.json`) — validating the shape BEFORE building
# that path closes off both a path-traversal read (e.g. "../instruments",
# which would resolve to `{root}/instruments.json`, a same-directory file
# with a completely different schema -> KeyError -> 500) and any other
# malformed input, final-fix-wave finding 2.
_BOOK_REF_RE = re.compile(r"^[0-9a-f]{12}$")


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
    # Option-leg fields (final-fix-wave finding 1) — null for a plain
    # equity/ETF leg, populated for an OPT leg pinned from an explicit
    # PositionIn. See write_book/read_book_positions below.
    strike: float | None = None
    expiry: str | None = None
    right: Literal["C", "P"] | None = None


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


def write_book(store, snapshot: BookSnapshot, legs: list[PositionIn] | None = None) -> None:
    """Persist an immutable snapshot as JSON (atomic tmp-then-replace, the
    same convention BarStore uses for its own writes — reproduced here as a
    small helper rather than imported, since store.py is A2's file this wave
    and this isn't a Parquet write).

    `legs` is the ORIGINAL posted `PositionIn` list (same order as
    `snapshot.portfolio.positions` — see `_portfolio_from_positions`, which
    builds one `Position` per `PositionIn` in list order), carrying
    strike/expiry/right. `None` is the live-broker auto-pin path: since
    2026-07-27 a broker `Position` carries the contract terms itself, so the
    fallback persists those — a real option leg from the live account pins
    complete and consumable. A broker leg IBKR somehow returned without
    terms still persists nulls and is refused at consumption by
    `read_book_positions`'s honest-refusal guard (final-fix-wave finding 1)."""
    path = _books_dir(store) / f"{snapshot.snapshot_id}.json"
    positions = snapshot.portfolio.positions
    leg_by_position = legs if legs is not None else [None] * len(positions)
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
                "strike": leg.strike if leg is not None else p.strike,
                "expiry": leg.expiry if leg is not None else p.expiry,
                "right": leg.right if leg is not None else p.right,
            }
            for p, leg in zip(positions, leg_by_position)
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def read_book(store, snapshot_id: str) -> dict:
    """Raw persisted payload for `snapshot_id`; 422 naming the ref if it was
    never pinned (matches whatif.py/hedge.py's unknown-symbol 422 policy —
    an unresolvable book_ref is a client error, never a 500). Also 422s a
    malformed ref (path-traversal-shaped or otherwise not a 12-hex-char
    snapshot id) BEFORE it ever becomes a filesystem path, and a corrupted
    snapshot file (never expected in normal operation, but a client should
    never see a 500 for it) — final-fix-wave finding 2."""
    if not _BOOK_REF_RE.match(snapshot_id):
        raise HTTPException(422, detail=f"invalid book_ref {snapshot_id!r}")
    path = _books_dir(store) / f"{snapshot_id}.json"
    if not path.exists():
        raise HTTPException(422, detail=f"unknown book_ref {snapshot_id!r}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(422, detail=f"corrupted book snapshot {snapshot_id!r}")


def read_book_positions(store, snapshot_id: str) -> list[PositionIn]:
    """Resolve a book_ref into the PositionIn legs whatif.py/hedge.py/
    options.py price off. Reconstructs the FULL leg (multiplier + the
    option-leg fields written by `write_book`, not just symbol/qty as before
    — final-fix-wave finding 1) so an option leg prices identically whether
    given inline or via book_ref.

    A persisted OPT leg (`sec_type == "OPT"`) with a null strike/expiry can
    only come from the live-broker auto-pin path (see `write_book`'s
    docstring) — there is no way to price it correctly, and silently
    treating it as a bare underlier position would be exactly the silent
    wrong-numbers bug this fix wave exists to close. Refuse it honestly
    instead.

    Batch-2 final review item 1: ANY persisted leg with a partially-set
    strike/expiry/right descriptor is refused regardless of sec_type —
    pin_book now enforces all-or-none at pin time, but snapshots written
    BEFORE that guard (e.g. sec_type STK with strike 450 and no right) still
    exist on disk and would otherwise silently misprice downstream."""
    payload = read_book(store, snapshot_id)
    positions: list[PositionIn] = []
    for p in payload["positions"]:
        if p.get("sec_type") == "OPT" and (p.get("strike") is None or p.get("expiry") is None):
            raise HTTPException(
                422,
                detail=(
                    f"snapshot's option legs lack strike/expiry ({p['symbol']!r}) — "
                    "re-pin with explicit legs"
                ),
            )
        descriptor = {"strike": p.get("strike"), "expiry": p.get("expiry"), "right": p.get("right")}
        given = [k for k, v in descriptor.items() if v is not None]
        if given and len(given) < len(descriptor):
            missing = [k for k, v in descriptor.items() if v is None]
            raise HTTPException(
                422,
                detail=(
                    f"snapshot leg {p['symbol']!r} has a partial option descriptor "
                    f"(has {'/'.join(given)}, missing {'/'.join(missing)}) — "
                    "re-pin with explicit legs"
                ),
            )
        positions.append(
            PositionIn(
                symbol=p["symbol"],
                qty=p["qty"],
                strike=p.get("strike"),
                expiry=p.get("expiry"),
                right=p.get("right"),
                multiplier=p.get("multiplier"),
            )
        )
    return positions


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


def _option_hash_extra(portfolio: Portfolio, legs: list[PositionIn] | None) -> str:
    """Fold strike/expiry/right into `BookSnapshot.create`'s hash (its
    `extra` parameter), sorted the same way the base hash sorts positions
    (by con_id) so the pairing stays correct: two books identical at the
    `Position` level (same con_id/qty/multiplier/sec_type) but differing
    only in an option leg's strike must NOT collide on the same snapshot id
    (final-fix-wave finding 1.iii) — the base hash alone can't tell them
    apart. `legs is None` is the live-broker path: since 2026-07-27 broker
    `Position`s carry the terms themselves, so the fold reads them off the
    positions (two live books differing only by an option leg's strike would
    otherwise collide the same way)."""
    if legs is None:
        ordered = sorted(portfolio.positions, key=lambda p: p.con_id)
        return "|".join(f"{p.strike}:{p.expiry}:{p.right}" for p in ordered)
    paired = sorted(zip(portfolio.positions, legs), key=lambda pl: pl[0].con_id)
    return "|".join(f"{leg.strike}:{leg.expiry}:{leg.right}" for _, leg in paired)


def _pin_and_respond(
    store,
    portfolio: Portfolio,
    valuation_ts: str,
    legs: list[PositionIn] | None = None,
    *,
    base_currency: str,
) -> BookSnapshotOut:
    extra = _option_hash_extra(portfolio, legs)
    # The REAL configured base currency (FX-aware valuation) — a GBP-based
    # account's snapshots pin GBP, never a hardcoded "USD".
    snapshot = BookSnapshot.create(
        portfolio, valuation_ts=valuation_ts, base_currency=base_currency, extra=extra
    )
    write_book(store, snapshot, legs)
    return _snapshot_out(read_book(store, snapshot.snapshot_id))


@router.post("/book/pin", response_model=BookSnapshotOut)
async def pin_book(request: Request, req: BookPinRequest) -> BookSnapshotOut:
    store = request.app.state.store
    base_currency = request.app.state.base_currency
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if req.positions is not None:
        # Pin-time all-or-none (Batch-2 final review item 1): a partial
        # strike/expiry/right descriptor must never PERSIST — refusing here
        # closes the hole for every downstream book_ref consumer at once
        # (whatif/hedge/lab/macro/portfolio/options), instead of each one
        # re-validating at consumption time.
        _validate_option_legs(req.positions, "pinned book")
        portfolio = _portfolio_from_positions(store, req.positions, valuation_ts)
        return _pin_and_respond(
            store, portfolio, valuation_ts, legs=req.positions, base_currency=base_currency
        )

    portfolio = await _live_portfolio(request, valuation_ts)
    return _pin_and_respond(store, portfolio, valuation_ts, base_currency=base_currency)


@router.get("/book/current", response_model=BookSnapshotOut)
async def get_current_book(request: Request) -> BookSnapshotOut:
    store = request.app.state.store
    base_currency = request.app.state.base_currency
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    portfolio = await _live_portfolio(request, valuation_ts)
    # auto-pin (wave-3 plan)
    return _pin_and_respond(store, portfolio, valuation_ts, base_currency=base_currency)


@router.get("/book/{snapshot_id}", response_model=BookSnapshotOut)
def get_book(snapshot_id: str, request: Request) -> BookSnapshotOut:
    store = request.app.state.store
    return _snapshot_out(read_book(store, snapshot_id))
