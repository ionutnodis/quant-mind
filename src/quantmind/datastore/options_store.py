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
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_META_PREFIX = b"quantmind.options."
_CHAIN_COLUMNS = frozenset(
    {"expiry", "strike", "right", "con_id", "bid", "ask", "iv", "delta", "multiplier"}
)


@dataclass(frozen=True)
class OptionsSnapshotMeta:
    as_of: str  # ISO date/timestamp the snapshot was taken
    spot: float  # underlying spot used for strike selection at snapshot time


class OptionsStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, underlier: str) -> Path:
        return self.root / "options" / f"{underlier}.parquet"

    def write_chain(self, underlier: str, quotes: pd.DataFrame, meta: OptionsSnapshotMeta) -> None:
        path = self._path(underlier)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(quotes, preserve_index=False)
        table = table.replace_schema_metadata(
            {
                **(table.schema.metadata or {}),
                _META_PREFIX + b"as_of": meta.as_of.encode(),
                _META_PREFIX + b"spot": repr(meta.spot).encode(),
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
            for key in (_META_PREFIX + b"as_of", _META_PREFIX + b"spot")
            if key not in md
        }
        if missing_metadata:
            raise ValueError(
                f"cached option chain for {underlier!r} is missing metadata"
            )
        meta = OptionsSnapshotMeta(
            as_of=md[_META_PREFIX + b"as_of"].decode(),
            spot=float(md[_META_PREFIX + b"spot"].decode()),
        )
        return table.to_pandas(), meta

    def has_chain(self, underlier: str) -> bool:
        return self._path(underlier).exists()
