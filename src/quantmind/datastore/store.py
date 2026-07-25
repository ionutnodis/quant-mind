"""Parquet bar store (Engineering Constraints 3, 4, 6, 10).

Layout: root/bars/{bar_size}/{con_id}.parquet — one file per instrument per bar
size, so a re-adjustment refresh rewrites one file, not the lake. Writes REPLACE
the file (adjusted history rewrites past bars after corporate actions). Bar
metadata (bar type, adjusted-as-of, RTH flag) travels in the parquet schema
metadata. Parquet is the source of truth; DuckDB reads these files. Single-writer
discipline is process-level (exactly one writer process per phase).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_META_PREFIX = b"quantmind."


@dataclass(frozen=True)
class BarMeta:
    bar_type: str  # e.g. "ADJUSTED_LAST" — risk math requires adjusted bars
    adjusted_asof: str
    rth_only: bool = True


class BarStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, con_id: int, bar_size: str) -> Path:
        return self.root / "bars" / bar_size / f"{con_id}.parquet"

    def write_bars(self, con_id: int, bar_size: str, bars: pd.DataFrame, meta: BarMeta) -> None:
        path = self._path(con_id, bar_size)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(bars, preserve_index=True)
        table = table.replace_schema_metadata(
            {
                **(table.schema.metadata or {}),
                _META_PREFIX + b"bar_type": meta.bar_type.encode(),
                _META_PREFIX + b"adjusted_asof": meta.adjusted_asof.encode(),
                _META_PREFIX + b"rth_only": str(meta.rth_only).encode(),
            }
        )
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp)
        tmp.replace(path)  # atomic replace: refresh semantics, never partial files

    def read_bars(self, con_id: int, bar_size: str) -> tuple[pd.DataFrame, BarMeta]:
        path = self._path(con_id, bar_size)
        if not path.exists():
            raise FileNotFoundError(f"no cached bars for con_id {con_id} at bar_size {bar_size}")
        table = pq.read_table(path)
        md = table.schema.metadata or {}
        meta = BarMeta(
            bar_type=md[_META_PREFIX + b"bar_type"].decode(),
            adjusted_asof=md[_META_PREFIX + b"adjusted_asof"].decode(),
            rth_only=md[_META_PREFIX + b"rth_only"].decode() == "True",
        )
        return table.to_pandas(), meta

    def watermark(self, con_id: int, bar_size: str) -> pd.Timestamp | None:
        """Last cached bar date — incremental sync fetches only newer bars (Constraint 6)."""
        path = self._path(con_id, bar_size)
        if not path.exists():
            return None
        bars, _ = self.read_bars(con_id, bar_size)
        return bars.index[-1]

    # --- symbol map: symbol -> conId, written by sync, read by the UI ---

    def write_symbol_map(self, mapping: dict[str, int]) -> None:
        import json

        path = self.root / "symbols.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, indent=2))
        tmp.replace(path)

    def read_symbol_map(self) -> dict[str, int]:
        import json

        path = self.root / "symbols.json"
        if not path.exists():
            return {}
        return {k: int(v) for k, v in json.loads(path.read_text()).items()}
