"""Best-effort UCITS profile enrichment for IBKR ETF listings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import sleep as _sleep

from quantmind.instruments.metadata import (
    ProfileFreshness,
    UcitsProfileResolutionV1,
    is_potential_ucits_isin,
    normalize_isin,
)


@dataclass(frozen=True)
class UcitsSyncStatus:
    symbol: str
    isin: str | None
    freshness: ProfileFreshness
    reason: str | None
    resolution: UcitsProfileResolutionV1 | None = None


def sync_ucits_profiles(
    store,
    instrument_metadata: dict[str, dict],
    provider,
    *,
    now: datetime,
    pace_seconds: float = 0.0,
    sleeper: Callable[[float], None] = _sleep,
) -> dict[str, UcitsSyncStatus]:
    """Enrich ETF listings without coupling profile failure to price sync.

    IBKR frequently exposes ETFs through ``secType=STK``; ``stock_type=ETF``
    is therefore the operational classifier.  The listing remains keyed by
    broker symbol/conId while the reusable share-class profile is keyed by
    ISIN, allowing multiple exchange listings to reference one profile.
    """

    if pace_seconds < 0:
        raise ValueError("pace_seconds must be non-negative")
    results: dict[str, UcitsSyncStatus] = {}
    metadata_updates: dict[str, dict] = {}
    checked_at = now.isoformat().replace("+00:00", "Z")
    candidates = [
        (symbol, fields)
        for symbol, fields in instrument_metadata.items()
        if str(fields.get("stock_type") or "").strip().upper() == "ETF"
        and is_potential_ucits_isin(fields.get("isin"))
    ]
    for index, (symbol, fields) in enumerate(candidates):
        raw_isin = fields.get("isin")
        try:
            isin = normalize_isin(raw_isin)
        except (TypeError, ValueError) as exc:
            reason = f"invalid or missing IBKR ISIN ({exc})"
            metadata_updates[symbol] = {
                "ucits_profile_status": ProfileFreshness.MISSING.value,
                "ucits_profile_checked_at": checked_at,
                "ucits_profile_reason": reason,
            }
            results[symbol] = UcitsSyncStatus(
                symbol=symbol,
                isin=None,
                freshness=ProfileFreshness.MISSING,
                reason=reason,
            )
            continue

        try:
            resolution = provider.resolve(isin, now=now)
        except Exception as exc:
            reason = f"UCITS provider failed ({type(exc).__name__})"
            status = UcitsSyncStatus(
                symbol=symbol,
                isin=isin,
                freshness=ProfileFreshness.MISSING,
                reason=reason,
            )
        else:
            reason = resolution.reason
            status = UcitsSyncStatus(
                symbol=symbol,
                isin=isin,
                freshness=resolution.freshness,
                reason=reason,
                resolution=resolution,
            )
        metadata_updates[symbol] = {
            "ucits_profile_isin": isin,
            "ucits_profile_status": status.freshness.value,
            "ucits_profile_checked_at": checked_at,
            "ucits_profile_reason": reason,
        }
        results[symbol] = status
        if pace_seconds and index < len(candidates) - 1:
            sleeper(pace_seconds)
    store.write_instrument_metadata_batch(metadata_updates)
    return results
