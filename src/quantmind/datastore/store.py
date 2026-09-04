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
_BAR_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})


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
        missing_columns = _BAR_COLUMNS - set(table.column_names)
        if missing_columns:
            raise ValueError(
                f"cached bars for con_id {con_id} are missing columns: "
                f"{sorted(missing_columns)}"
            )
        md = table.schema.metadata or {}
        meta = BarMeta(
            bar_type=md[_META_PREFIX + b"bar_type"].decode(),
            adjusted_asof=md[_META_PREFIX + b"adjusted_asof"].decode(),
            rth_only=md[_META_PREFIX + b"rth_only"].decode() == "True",
        )
        return table.to_pandas(), meta

    def watermark(self, con_id: int, bar_size: str) -> pd.Timestamp | None:
        """Last cached bar date without decoding the full OHLCV table.

        Setup polls readiness, and incremental sync calls this for every symbol.
        Reading only the persisted pandas index keeps that check proportional to
        one narrow timestamp column rather than the entire market cache.
        """
        path = self._path(con_id, bar_size)
        if path.exists():
            parquet = pq.ParquetFile(path)
            missing_columns = _BAR_COLUMNS - set(parquet.schema_arrow.names)
            if missing_columns:
                raise ValueError(
                    f"cached bars for con_id {con_id} are missing columns: "
                    f"{sorted(missing_columns)}"
                )
        return self._index_watermark(
            path,
            fallback=lambda: self.read_bars(con_id, bar_size)[0].index,
        )

    @staticmethod
    def _index_watermark(path: Path, fallback) -> pd.Timestamp | None:
        """Read the last persisted pandas index value from one Parquet file."""
        if not path.exists():
            return None
        parquet = pq.ParquetFile(path)
        pandas_metadata = parquet.schema_arrow.pandas_metadata or {}
        index_columns = pandas_metadata.get("index_columns", [])
        index_name = index_columns[0] if index_columns else None
        if isinstance(index_name, str):
            last_value = None
            for batch in parquet.iter_batches(columns=[index_name]):
                if batch.num_rows:
                    last_value = batch.column(0)[batch.num_rows - 1].as_py()
            return None if last_value is None else pd.Timestamp(last_value)

        # A RangeIndex has no physical Parquet column. Bar data always uses a
        # DatetimeIndex, but retain a compatibility fallback for older files.
        index = fallback()
        return index[-1] if len(index) else None

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

    def write_required_symbols(self, symbols: list[str]) -> None:
        """Persist the universe the latest complete sync intended to serve.

        The broader symbol map is intentionally merge-preserved for cached
        history and research. Readiness must not let those orphaned mappings
        block a current book forever, so it reads this narrower manifest.
        """
        import json

        path = self.root / "required_symbols.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    def read_required_symbols(self) -> list[str]:
        import json

        path = self.root / "required_symbols.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text())
        if not isinstance(payload, list) or not all(
            isinstance(symbol, str) and symbol for symbol in payload
        ):
            raise ValueError("required_symbols.json must contain non-empty symbols")
        return list(dict.fromkeys(payload))

    # --- generic named series (FRED etc.): root/series/{name}.parquet ---

    def _series_path(self, name: str):
        return self.root / "series" / f"{name}.parquet"

    def write_series(self, name: str, series: pd.Series) -> None:
        path = self._series_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(series.rename("value").to_frame(), preserve_index=True)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp)
        tmp.replace(path)

    def read_series(self, name: str) -> pd.Series:
        path = self._series_path(name)
        if not path.exists():
            raise FileNotFoundError(f"no cached series {name!r}")
        df = pq.read_table(path).to_pandas()
        return df["value"]

    def series_watermark(self, name: str) -> pd.Timestamp | None:
        """Last named-series date without decoding the full value column."""
        return self._index_watermark(
            self._series_path(name),
            fallback=lambda: self.read_series(name).index,
        )

    def list_series(self) -> list[str]:
        d = self.root / "series"
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.parquet"))

    # --- instrument metadata (Task A2): symbol -> {con_id, long_name,
    # exchange, currency, sec_type, industry, region, provider}, cached at
    # sync from IBKR contract details (or recorded provider="yfinance" for
    # the free-fallback path). One JSON file, merge-write per symbol so a
    # later refresh (e.g. a new region tag) never clobbers fields a previous
    # sync already wrote — single-provenance law lives in the `provider` field.

    def _instruments_path(self) -> Path:
        return self.root / "instruments.json"

    def write_instrument_metadata(self, symbol: str, fields: dict) -> None:
        import json

        path = self._instruments_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        all_meta = self.read_all_instrument_metadata()
        all_meta[symbol] = {**all_meta.get(symbol, {}), **fields}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(all_meta, indent=2))
        tmp.replace(path)

    def read_instrument_metadata(self, symbol: str) -> dict | None:
        return self.read_all_instrument_metadata().get(symbol)

    def read_all_instrument_metadata(self) -> dict[str, dict]:
        import json

        path = self._instruments_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())
