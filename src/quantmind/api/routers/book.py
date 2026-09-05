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

`GET /api/book/current` is a read-only preview of the live broker book.
`POST /api/book/pin` is the only operation that persists a snapshot and mints
a `book_ref` usable by downstream analysis.
"""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from quantmind.api.routers._shared import (
    PositionIn,
    mapped_instrument_metadata,
    read_instrument_metadata_map,
)
from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio, Position, option_terms_complete

router = APIRouter()

# Snapshot ids are always a 12-hex-char sha256 prefix (BookSnapshot.create).
# book_ref is client-controlled and lands directly in a filesystem path
# (`{root}/books/{snapshot_id}.json`) — validating the shape BEFORE building
# that path closes off both a path-traversal read (e.g. "../instruments",
# which would resolve to `{root}/instruments.json`, a same-directory file
# with a completely different schema -> KeyError -> 500) and any other
# malformed input, final-fix-wave finding 2.
_BOOK_REF_RE = re.compile(r"^[0-9a-f]{12}$")


class SnapshotCollisionError(RuntimeError):
    """An existing immutable ID points at non-identical snapshot content."""


class BookPinRequest(BaseModel):
    # None -> pin the live broker's current book. A posted list pins that
    # instead — including [] when the caller explicitly confirms an empty
    # hypothetical book.
    positions: list[PositionIn] | None = None


class BookPositionOut(BaseModel):
    symbol: str
    qty: float
    con_id: int | None
    sec_type: str
    multiplier: float
    # Option-leg fields (final-fix-wave finding 1) — null for a plain
    # equity/ETF leg, populated for a complete OPT leg. Instrument identity
    # comes from live IBKR contracts and remains null for legacy/manual books.
    strike: float | None = None
    expiry: str | None = None
    right: Literal["C", "P"] | None = None
    currency: str | None = None
    exchange: str | None = None


class BookSnapshotOut(BaseModel):
    snapshot_id: str
    valuation_ts: str
    base_currency: str
    positions: list[BookPositionOut]
    source: Literal["live_ibkr", "manual", "legacy"] = "legacy"
    account_fingerprint: str | None = None
    broker_mode: Literal["paper", "live", "custom"] | None = None
    rebased_from: str | None = None


class CurrentBookOut(BaseModel):
    """A live broker preview. It is deliberately not a persisted snapshot."""

    valuation_ts: str
    base_currency: str
    positions: list[BookPositionOut]


class BookConflictOut(BaseModel):
    """Stable response body for immutable-book identity conflicts."""

    detail: str


class ResolvedBookPosition(PositionIn):
    """Internal pinned-book leg with its persisted contract identity/type."""

    con_id: int | None = None
    sec_type: str = "STK"


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


def _account_fingerprint(account_id: str | None) -> str | None:
    """Stable local scope marker without persisting a brokerage account id."""
    if not account_id:
        return None
    return hashlib.sha256(account_id.encode()).hexdigest()[:12]


def validate_pinned_book_scope(
    state, pinned: dict, *, require_configured_base: bool = True
) -> None:
    """Reject a live snapshot outside the currently selected broker scope.

    Manual books are intentionally portable. A broker-sourced book is not:
    reusing its URL after switching account or paper/live mode would run a
    valid calculation against the wrong holdings.
    """
    configured_base = getattr(state, "base_currency", "USD")
    if require_configured_base and pinned.get("base_currency") != configured_base:
        raise HTTPException(
            409,
            detail=(
                f"pinned book uses base currency {pinned.get('base_currency')}; "
                f"rebase or re-pin the book for configured base currency {configured_base}"
            ),
        )
    if pinned["source"] != "live_ibkr":
        return
    current_fingerprint = _account_fingerprint(
        getattr(state, "broker_account_id", None)
    )
    if (
        pinned["account_fingerprint"] is None
        or current_fingerprint is None
        or pinned["account_fingerprint"] != current_fingerprint
    ):
        raise HTTPException(
            409,
            detail="pinned book account does not match the current broker account",
        )
    if pinned["broker_mode"] != getattr(state, "broker_mode", None):
        raise HTTPException(
            409,
            detail="pinned book broker mode does not match the current broker mode",
        )


def validate_pinned_instrument_identities(
    store, pinned: dict, positions: list[ResolvedBookPosition]
) -> None:
    """Fail closed when a pinned symbol now resolves to another contract.

    Symbol maps are mutable ingestion state, while a book_ref promises an
    immutable analytical identity. Stock conIds (and manual option underlier
    conIds) must therefore still match before any downstream route prices the
    book. Live IBKR option conIds identify the option contract itself and are
    checked against cached chains by the options/portfolio routes instead.
    """
    try:
        symbol_map = store.read_symbol_map()
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            422,
            detail="symbol map is corrupt; run sync to rebuild it",
        ) from exc
    changed = sorted(
        {
            position.symbol
            for position in positions
            if (
                position.sec_type == "STK"
                or pinned.get("source") != "live_ibkr"
            )
            and (
                position.con_id is None
                or symbol_map.get(position.symbol) != position.con_id
            )
        }
    )
    if changed:
        raise HTTPException(
            409,
            detail=(
                "pinned book instrument identity changed for "
                f"{changed}; re-pin the book before analysis"
            ),
        )


def validate_live_stock_identities(store, portfolio: Portfolio) -> None:
    """Refuse to collapse broker-held stock listings onto one ticker key."""
    identities: dict[str, set[int]] = {}
    for position in portfolio.positions:
        if position.sec_type == "STK":
            identities.setdefault(position.symbol, set()).add(position.con_id)
    duplicate_listings = sorted(
        symbol for symbol, con_ids in identities.items() if len(con_ids) > 1
    )
    if duplicate_listings:
        raise HTTPException(
            422,
            detail=(
                "live portfolio contains multiple listings for the same ticker "
                f"{duplicate_listings}; this release cannot safely collapse them. "
                "Use one canonical listing per ticker before analysis."
            ),
        )
    try:
        symbol_map = store.read_symbol_map()
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            422,
            detail="symbol map is corrupt; run sync to rebuild it",
        ) from exc
    changed = sorted(
        symbol
        for symbol, con_ids in identities.items()
        if symbol in symbol_map
        and symbol_map[symbol] != next(iter(con_ids))
    )
    if changed:
        raise HTTPException(
            409,
            detail=(
                "live portfolio contract identity does not match cached market data "
                f"for {changed}; run sync before analysis"
            ),
        )


def write_book(
    store,
    snapshot: BookSnapshot,
    legs: list[PositionIn] | None = None,
    *,
    source: Literal["live_ibkr", "manual", "legacy"] = "legacy",
    account_fingerprint: str | None = None,
    broker_mode: Literal["paper", "live", "custom"] | None = None,
    identity_version: Literal["book_snapshot_v2"] | None = None,
    rebased_from: str | None = None,
) -> None:
    """Persist an immutable snapshot as JSON (atomic tmp-then-replace, the
    same convention BarStore uses for its own writes — reproduced here as a
    small helper rather than imported, since store.py is A2's file this wave
    and this isn't a Parquet write).

    `legs` is the ORIGINAL posted `PositionIn` list (same order as
    `snapshot.portfolio.positions` — see `_portfolio_from_positions`, which
    builds one `Position` per `PositionIn` in list order). Posted legs remain
    authoritative on that path; the live-broker path reads option contract
    identity directly from `Position`, where the IBKR adapter preserves it.
    Incomplete legacy option positions remain null and are refused by
    `read_book_positions` rather than silently treated as stock."""
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
                "currency": p.currency,
                "exchange": p.exchange,
            }
            for p, leg in zip(positions, leg_by_position)
        ],
    }
    if identity_version is not None:
        payload["identity_version"] = identity_version
        payload["source"] = source
        payload["account_fingerprint"] = account_fingerprint
        payload["broker_mode"] = broker_mode
    elif (
        source != "legacy"
        or account_fingerprint is not None
        or broker_mode is not None
        or rebased_from is not None
    ):
        raise ValueError("provenance-scoped snapshots require book_snapshot_v2")
    if rebased_from is not None:
        payload["rebased_from"] = rebased_from
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing == payload:
            return
        raise SnapshotCollisionError(
            f"immutable snapshot collision for {snapshot.snapshot_id}"
        )
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
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise TypeError("snapshot payload must be an object")
        identity_version = payload.get("identity_version")
        if identity_version == "book_snapshot_v2":
            _verify_snapshot_identity(payload)
        elif identity_version is None:
            _verify_legacy_snapshot_identity(payload)
        else:
            raise ValueError("unsupported snapshot identity version")
        snapshot = _snapshot_out(payload)
        if snapshot.snapshot_id != snapshot_id:
            raise ValueError("snapshot id does not match filename")
        return snapshot.model_dump()
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise HTTPException(422, detail=f"corrupted book snapshot {snapshot_id!r}")


def list_books(store) -> list[BookSnapshotOut]:
    """Return every schema-valid persisted book, ignoring corrupt files."""
    snapshots: list[BookSnapshotOut] = []
    for path in _books_dir(store).glob("*.json"):
        if not _BOOK_REF_RE.match(path.stem):
            continue
        try:
            snapshots.append(_snapshot_out(read_book(store, path.stem)))
        except HTTPException:
            continue
    return snapshots


def read_book_positions(store, snapshot_id: str) -> list[ResolvedBookPosition]:
    """Resolve a book_ref into the PositionIn legs whatif.py/hedge.py/
    options.py price off. Reconstructs the FULL leg (multiplier + the
    option-leg fields written by `write_book`, not just symbol/qty as before
    — final-fix-wave finding 1) so an option leg prices identically whether
    given inline or via book_ref.

    A persisted OPT leg (`sec_type == "OPT"`) with a null strike/expiry comes
    from an older or incomplete broker position — there is no way to price it
    correctly, and silently
    treating it as a bare underlier position would be exactly the silent
    wrong-numbers bug this fix wave exists to close. Refuse it honestly
    instead."""
    payload = read_book(store, snapshot_id)
    unknown_currencies = sorted(
        {
            p.get("currency") or "UNKNOWN"
            for p in payload["positions"]
            if not p.get("currency") or p.get("currency") == "UNKNOWN"
        }
    )
    if unknown_currencies:
        raise HTTPException(
            422,
            detail=(
                "FX normalization requires known pinned currencies: "
                f"{unknown_currencies}"
            ),
        )
    positions: list[ResolvedBookPosition] = []
    for p in payload["positions"]:
        if p.get("sec_type") == "OPT" and not option_terms_complete(
            strike=p.get("strike"), expiry=p.get("expiry"), right=p.get("right")
        ):
            raise HTTPException(
                422,
                detail=(
                    f"snapshot's option legs lack strike/expiry/right ({p['symbol']!r}) — "
                    "re-pin with explicit legs"
                ),
            )
        positions.append(
            ResolvedBookPosition(
                con_id=p.get("con_id"),
                sec_type=p.get("sec_type", "STK"),
                symbol=p["symbol"],
                qty=p["qty"],
                strike=p.get("strike"),
                expiry=p.get("expiry"),
                right=p.get("right"),
                multiplier=p.get("multiplier"),
                currency=p.get("currency"),
                exchange=p.get("exchange"),
            )
        )
    return positions


def _snapshot_out(payload: dict) -> BookSnapshotOut:
    return BookSnapshotOut(
        snapshot_id=payload["snapshot_id"],
        valuation_ts=payload["valuation_ts"],
        base_currency=payload["base_currency"],
        positions=[BookPositionOut(**p) for p in payload["positions"]],
        source=payload.get("source", "legacy"),
        account_fingerprint=payload.get("account_fingerprint"),
        broker_mode=payload.get("broker_mode"),
        rebased_from=payload.get("rebased_from"),
    )


async def _live_portfolio(request: Request) -> Portfolio:
    broker = request.app.state.broker
    if broker is None:
        raise HTTPException(
            503,
            detail="broker unavailable; connect a broker or provide positions explicitly",
        )
    return await broker.get_portfolio()


def _current_out(
    portfolio: Portfolio, valuation_ts: str, *, base_currency: str = "USD"
) -> CurrentBookOut:
    return CurrentBookOut(
        valuation_ts=valuation_ts,
        base_currency=base_currency,
        positions=[
            BookPositionOut(
                symbol=p.symbol,
                qty=p.qty,
                con_id=p.con_id,
                sec_type=p.sec_type,
                multiplier=p.multiplier,
                strike=p.strike,
                expiry=p.expiry,
                right=p.right,
                currency=p.currency,
                exchange=p.exchange,
            )
            for p in portfolio.positions
        ],
    )


def _portfolio_from_positions(store, positions: list[PositionIn], valuation_ts: str) -> Portfolio:
    symbol_map = store.read_symbol_map()
    unknown = sorted({p.symbol for p in positions} - symbol_map.keys())
    if unknown:
        raise HTTPException(422, detail=f"unknown symbols: {unknown}")
    metadata_by_symbol = read_instrument_metadata_map(store) if positions else {}
    resolved_positions: list[Position] = []
    for p in positions:
        metadata = mapped_instrument_metadata(
            metadata_by_symbol, symbol_map, p.symbol
        )
        authoritative_currency = str(metadata.get("currency") or "UNKNOWN")
        authoritative_exchange = str(metadata.get("exchange") or "").strip() or None
        if p.currency is not None and p.currency != authoritative_currency:
            raise HTTPException(
                422,
                detail=(
                    f"currency for {p.symbol!r} does not match cached instrument identity"
                ),
            )
        resolved_positions.append(
            Position(
                con_id=symbol_map[p.symbol],
                symbol=p.symbol,
                qty=p.qty,
                sec_type="OPT" if p.right is not None else "STK",
                multiplier=_leg_multiplier(p),
                strike=p.strike,
                expiry=p.expiry,
                right=p.right,
                currency=authoritative_currency,
                exchange=authoritative_exchange,
            )
        )
    return Portfolio(positions=tuple(resolved_positions), as_of=valuation_ts)


def _snapshot_hash_extra(
    portfolio: Portfolio,
    legs: list[PositionIn] | None,
    *,
    source: Literal["live_ibkr", "manual", "legacy"],
    account_fingerprint: str | None,
    broker_mode: Literal["paper", "live", "custom"] | None,
    rebased_from: str | None = None,
) -> str:
    """Fold complete contract identity and provenance scope into the ID."""
    leg_by_position = legs if legs is not None else [None] * len(portfolio.positions)
    identities = []
    for position, leg in zip(portfolio.positions, leg_by_position):
        identities.append(
            {
                "con_id": position.con_id,
                "symbol": position.symbol,
                "qty": position.qty,
                "sec_type": position.sec_type,
                "multiplier": position.multiplier,
                "strike": leg.strike if leg is not None else position.strike,
                "expiry": leg.expiry if leg is not None else position.expiry,
                "right": leg.right if leg is not None else position.right,
                "currency": position.currency,
                "exchange": position.exchange,
            }
        )
    identities.sort(key=lambda identity: json.dumps(identity, sort_keys=True))
    scope = {
        "positions": identities,
        "source": source,
        "account_fingerprint": account_fingerprint,
        "broker_mode": broker_mode,
    }
    # Preserve the exact v2 identity preimage for existing snapshots; lineage
    # is folded in only for a newly rebased successor.
    if rebased_from is not None:
        scope["rebased_from"] = rebased_from
    return json.dumps(
        scope,
        sort_keys=True,
        separators=(",", ":"),
    )


def _verify_snapshot_identity(payload: dict) -> None:
    """Recompute v2 content identity before trusting persisted book data."""
    positions = tuple(
        Position(
            con_id=int(item["con_id"]),
            symbol=item["symbol"],
            qty=item["qty"],
            sec_type=item["sec_type"],
            multiplier=item["multiplier"],
            strike=item.get("strike"),
            expiry=item.get("expiry"),
            right=item.get("right"),
            currency=item.get("currency"),
            exchange=item.get("exchange"),
        )
        for item in payload["positions"]
    )
    portfolio = Portfolio(positions=positions, as_of=payload["valuation_ts"])
    extra = _snapshot_hash_extra(
        portfolio,
        None,
        source=payload["source"],
        account_fingerprint=payload.get("account_fingerprint"),
        broker_mode=payload.get("broker_mode"),
        rebased_from=payload.get("rebased_from"),
    )
    expected = BookSnapshot.create(
        portfolio,
        valuation_ts=payload["valuation_ts"],
        base_currency=payload["base_currency"],
        extra=extra,
    )
    if expected.snapshot_id != payload["snapshot_id"]:
        raise ValueError("snapshot content hash does not match snapshot_id")


def _verify_legacy_snapshot_identity(payload: dict) -> None:
    """Verify pre-v2 content hashes and reject marker-removal downgrade attempts."""
    if any(
        field in payload
        for field in ("source", "account_fingerprint", "broker_mode", "rebased_from")
    ):
        raise ValueError("unversioned snapshot contains v2-only provenance fields")
    positions = tuple(
        Position(
            con_id=int(item["con_id"]),
            symbol=item["symbol"],
            qty=item["qty"],
            sec_type=item["sec_type"],
            multiplier=item["multiplier"],
        )
        for item in payload["positions"]
    )
    portfolio = Portfolio(positions=positions, as_of=payload["valuation_ts"])
    option_identities = [
        (
            item.get("strike"),
            item.get("expiry"),
            item.get("right"),
        )
        for item in sorted(payload["positions"], key=lambda item: int(item["con_id"]))
    ]
    extra = (
        "|".join(f"{strike}:{expiry}:{right}" for strike, expiry, right in option_identities)
        if any(any(value is not None for value in identity) for identity in option_identities)
        else ""
    )
    expected = BookSnapshot.create(
        portfolio,
        valuation_ts=payload["valuation_ts"],
        base_currency=payload["base_currency"],
        extra=extra,
    )
    if expected.snapshot_id != payload["snapshot_id"]:
        raise ValueError("legacy snapshot content hash does not match snapshot_id")


def _pin_and_respond(
    store,
    portfolio: Portfolio,
    valuation_ts: str,
    legs: list[PositionIn] | None = None,
    *,
    source: Literal["live_ibkr", "manual", "legacy"] = "manual",
    account_fingerprint: str | None = None,
    broker_mode: Literal["paper", "live", "custom"] | None = None,
    base_currency: str = "USD",
    rebased_from: str | None = None,
) -> BookSnapshotOut:
    extra = _snapshot_hash_extra(
        portfolio,
        legs,
        source=source,
        account_fingerprint=account_fingerprint,
        broker_mode=broker_mode,
        rebased_from=rebased_from,
    )
    snapshot = BookSnapshot.create(
        portfolio,
        valuation_ts=valuation_ts,
        base_currency=base_currency,
        extra=extra,
    )
    try:
        write_book(
            store,
            snapshot,
            legs,
            source=source,
            account_fingerprint=account_fingerprint,
            broker_mode=broker_mode,
            identity_version="book_snapshot_v2",
            rebased_from=rebased_from,
        )
    except SnapshotCollisionError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return _snapshot_out(read_book(store, snapshot.snapshot_id))


@router.post("/book/pin", response_model=BookSnapshotOut)
async def pin_book(request: Request, req: BookPinRequest) -> BookSnapshotOut:
    store = request.app.state.store
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if req.positions is not None:
        portfolio = _portfolio_from_positions(store, req.positions, valuation_ts)
        return _pin_and_respond(
            store,
            portfolio,
            valuation_ts,
            legs=req.positions,
            source="manual",
            base_currency=request.app.state.base_currency,
        )

    portfolio = await _live_portfolio(request)
    validate_live_stock_identities(store, portfolio)
    return _pin_and_respond(
        store,
        portfolio,
        valuation_ts,
        source="live_ibkr",
        account_fingerprint=_account_fingerprint(
            getattr(request.app.state, "broker_account_id", None)
        ),
        broker_mode=getattr(request.app.state, "broker_mode", None),
        base_currency=request.app.state.base_currency,
    )


@router.get("/book/current", response_model=CurrentBookOut)
async def get_current_book(request: Request) -> CurrentBookOut:
    valuation_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    portfolio = await _live_portfolio(request)
    validate_live_stock_identities(request.app.state.store, portfolio)
    return _current_out(
        portfolio, valuation_ts, base_currency=request.app.state.base_currency
    )


@router.post(
    "/book/{snapshot_id}/rebase",
    response_model=BookSnapshotOut,
    responses={
        409: {
            "model": BookConflictOut,
            "description": "Pinned-book identity or immutable snapshot conflict",
        }
    },
)
def rebase_book(snapshot_id: str, request: Request) -> BookSnapshotOut:
    """Mint an immutable reporting-currency successor without rewriting history."""
    store = request.app.state.store
    payload = read_book(store, snapshot_id)
    validate_pinned_book_scope(
        request.app.state,
        payload,
        require_configured_base=False,
    )
    resolved = read_book_positions(store, snapshot_id)
    validate_pinned_instrument_identities(store, payload, resolved)
    target_base = request.app.state.base_currency
    if payload["base_currency"] == target_base:
        return _snapshot_out(payload)

    portfolio = Portfolio(
        positions=tuple(
            Position(
                con_id=position.con_id,
                symbol=position.symbol,
                qty=position.qty,
                sec_type=position.sec_type,
                multiplier=_leg_multiplier(position),
                strike=position.strike,
                expiry=position.expiry,
                right=position.right,
                currency=position.currency,
                exchange=position.exchange,
            )
            for position in resolved
        ),
        as_of=payload["valuation_ts"],
    )
    return _pin_and_respond(
        store,
        portfolio,
        payload["valuation_ts"],
        source=payload["source"],
        account_fingerprint=payload.get("account_fingerprint"),
        broker_mode=payload.get("broker_mode"),
        base_currency=target_base,
        rebased_from=snapshot_id,
    )


@router.get("/book/{snapshot_id}", response_model=BookSnapshotOut)
def get_book(snapshot_id: str, request: Request) -> BookSnapshotOut:
    store = request.app.state.store
    return _snapshot_out(read_book(store, snapshot_id))
