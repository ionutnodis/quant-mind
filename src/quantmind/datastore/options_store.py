"""Options chain parquet store (Task A3 — owned; NOT part of BarStore, per
wave-3 ownership split: A2 owns `store.py`).

Layout: root/options/{underlier}.parquet — one file per underlier. A snapshot
REPLACES the file entirely: unlike adjusted bars (Engineering Constraint 3,
append/merge semantics), the chain sleeve has no append-only history
requirement in v1 — a re-sync's strikes/expiries shift with spot and time, so
the prior snapshot is simply stale, not a partial fact to merge. as_of + spot
travel in the parquet schema metadata (same technique as BarStore's BarMeta).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from numbers import Integral
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_META_PREFIX = b"quantmind.options."
_CHAIN_COLUMNS = frozenset(
    {
        "expiry",
        "strike",
        "right",
        "con_id",
        "bid",
        "ask",
        "iv",
        "delta",
        "multiplier",
        "observed_at",
        "market_data_type",
    }
)


@dataclass(frozen=True)
class OptionsSnapshotMeta:
    as_of: str  # ISO date/timestamp the snapshot was taken
    spot: float  # underlying spot used for strike selection at snapshot time
    underlier_con_id: int  # authoritative contract used to discover the chain


def option_chain_freshness(
    as_of: str,
    today: date,
    *,
    stale_after_business_days: int = 3,
) -> tuple[int | None, bool]:
    """Return weekday-aware snapshot age and whether it exceeds the limit."""
    try:
        snapshot_date = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    except ValueError:
        return None, True
    if snapshot_date > today:
        return None, True
    age_days = int(np.busday_count(snapshot_date.isoformat(), today.isoformat()))
    return age_days, age_days > stale_after_business_days


class OptionsStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, underlier: str) -> Path:
        return self.root / "options" / f"{underlier}.parquet"

    @staticmethod
    def _validate_quote_evidence(
        underlier: str,
        quotes: pd.DataFrame,
        meta: OptionsSnapshotMeta,
    ) -> None:
        missing_columns = _CHAIN_COLUMNS - set(quotes.columns)
        if missing_columns:
            raise ValueError(
                f"cached option chain for {underlier!r} is missing columns: "
                f"{sorted(missing_columns)}"
            )
        try:
            snapshot_time = datetime.fromisoformat(meta.as_of.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ValueError(
                f"cached option chain for {underlier!r} has an invalid snapshot timestamp"
            )
        if snapshot_time.tzinfo is None:
            raise ValueError(
                f"cached option chain for {underlier!r} snapshot timestamp "
                "must be timezone-aware"
            )
        snapshot_time = snapshot_time.astimezone(timezone.utc)

        observation_times: list[datetime] = []
        for row in quotes.itertuples(index=False):
            raw_observed_at = row.observed_at
            try:
                observed_at = datetime.fromisoformat(
                    str(raw_observed_at).replace("Z", "+00:00")
                )
            except ValueError:
                raise ValueError(
                    f"cached option quote for {underlier!r} has an invalid observed_at"
                )
            if observed_at.tzinfo is None:
                raise ValueError(
                    f"cached option quote for {underlier!r} observed_at must be timezone-aware"
                )
            observation_times.append(observed_at.astimezone(timezone.utc))

            market_data_type = row.market_data_type
            if (
                not isinstance(market_data_type, Integral)
                or isinstance(market_data_type, bool)
                or int(market_data_type) not in {1, 2, 3, 4}
            ):
                raise ValueError(
                    f"cached option quote for {underlier!r} has an invalid market_data_type"
                )

        if observation_times and min(observation_times) != snapshot_time:
            raise ValueError(
                f"cached option chain for {underlier!r} snapshot timestamp must equal "
                "its weakest quote observation"
            )

    def write_chain(self, underlier: str, quotes: pd.DataFrame, meta: OptionsSnapshotMeta) -> None:
        self._validate_quote_evidence(underlier, quotes, meta)
        path = self._path(underlier)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(quotes, preserve_index=False)
        table = table.replace_schema_metadata(
            {
                **(table.schema.metadata or {}),
                _META_PREFIX + b"as_of": meta.as_of.encode(),
                _META_PREFIX + b"spot": repr(meta.spot).encode(),
                _META_PREFIX + b"underlier_con_id": str(meta.underlier_con_id).encode(),
            }
        )
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp)
        tmp.replace(path)  # atomic replace: a snapshot always fully supersedes the last one

    def read_chain(self, underlier: str) -> tuple[pd.DataFrame, OptionsSnapshotMeta]:
        path = self._path(underlier)
        if not path.exists():
            raise FileNotFoundError(f"no cached option chain for underlier {underlier!r}")
        table = pq.read_table(path)
        missing_columns = _CHAIN_COLUMNS - set(table.column_names)
        if missing_columns:
            raise ValueError(
                f"cached option chain for {underlier!r} is missing columns: "
                f"{sorted(missing_columns)}"
            )
        md = table.schema.metadata or {}
        missing_metadata = {
            key
            for key in (
                _META_PREFIX + b"as_of",
                _META_PREFIX + b"spot",
                _META_PREFIX + b"underlier_con_id",
            )
            if key not in md
        }
        if missing_metadata:
            raise ValueError(
                f"cached option chain for {underlier!r} is missing metadata"
            )
        meta = OptionsSnapshotMeta(
            as_of=md[_META_PREFIX + b"as_of"].decode(),
            spot=float(md[_META_PREFIX + b"spot"].decode()),
            underlier_con_id=int(md[_META_PREFIX + b"underlier_con_id"].decode()),
        )
        quotes = table.to_pandas()
        self._validate_quote_evidence(underlier, quotes, meta)
        return quotes, meta

    def has_chain(self, underlier: str) -> bool:
        return self._path(underlier).exists()
