"""Parquet bar store (Engineering Constraints 3, 4, 6, 10).

Layout: root/bars/{bar_size}/{con_id}.parquet — one file per instrument per bar
size, so a re-adjustment refresh rewrites one file, not the lake. Writes REPLACE
the file (adjusted history rewrites past bars after corporate actions). Bar
metadata (bar type, adjusted-as-of, RTH flag) travels in the parquet schema
metadata. Parquet is the source of truth; DuckDB reads these files. Single-writer
discipline is process-level (exactly one writer process per phase).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from quantmind.instruments.metadata import UcitsEtfProfileV1

_META_PREFIX = b"quantmind."
_BAR_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})
_INSTRUMENT_METADATA_SHAPE_ERROR = (
    "instruments.json must map symbols to metadata objects"
)
_INSTRUMENT_METADATA_STRING_FIELDS = frozenset(
    {
        "currency",
        "exchange",
        "industry",
        "isin",
        "issuer_id",
        "local_symbol",
        "long_name",
        "primary_exchange",
        "provider",
        "quote_unit",
        "region",
        "sec_type",
        "stock_type",
        "trading_class",
        "ucits_profile_isin",
        "ucits_profile_reason",
    }
)
_UCITS_PROFILE_STATUSES = frozenset({"FRESH", "STALE", "MISSING"})


def _instrument_metadata_value_error(
    symbol: str, field: str, expected: str
) -> ValueError:
    return ValueError(
        f"instruments.json contains invalid metadata for {symbol!r}: "
        f"{field!r} must be {expected}"
    )


def _validate_instrument_metadata(payload: object) -> dict[str, dict]:
    if not isinstance(payload, dict) or not all(
        isinstance(symbol, str) and isinstance(fields, dict)
        for symbol, fields in payload.items()
    ):
        raise ValueError(_INSTRUMENT_METADATA_SHAPE_ERROR)

    for symbol, fields in payload.items():
        for field in _INSTRUMENT_METADATA_STRING_FIELDS:
            value = fields.get(field)
            if value is not None and not isinstance(value, str):
                raise _instrument_metadata_value_error(
                    symbol, field, "a string or null"
                )

        con_id = fields.get("con_id")
        if con_id is not None and (
            not isinstance(con_id, int) or isinstance(con_id, bool)
        ):
            raise _instrument_metadata_value_error(
                symbol, "con_id", "an integer or null"
            )

        valid_exchanges = fields.get("valid_exchanges")
        if valid_exchanges is not None and (
            not isinstance(valid_exchanges, list)
            or not all(isinstance(exchange, str) for exchange in valid_exchanges)
        ):
            raise _instrument_metadata_value_error(
                symbol, "valid_exchanges", "a list of strings or null"
            )

        external_identifiers = fields.get("external_identifiers")
        if external_identifiers is not None and (
            not isinstance(external_identifiers, dict)
            or not all(
                isinstance(namespace, str)
                and isinstance(values, list)
                and all(isinstance(value, str) for value in values)
                for namespace, values in external_identifiers.items()
            )
        ):
            raise _instrument_metadata_value_error(
                symbol,
                "external_identifiers",
                "an object mapping namespaces to lists of strings or null",
            )

        profile_status = fields.get("ucits_profile_status")
        if (
            profile_status is not None
            and profile_status not in _UCITS_PROFILE_STATUSES
        ):
            raise _instrument_metadata_value_error(
                symbol,
                "ucits_profile_status",
                "FRESH, STALE, MISSING, or null",
            )

        price_scale = fields.get("price_scale")
        if price_scale is not None and (
            isinstance(price_scale, bool)
            or not isinstance(price_scale, (int, float))
            or not math.isfinite(price_scale)
            or price_scale <= 0
        ):
            raise _instrument_metadata_value_error(
                symbol, "price_scale", "a positive finite number or null"
            )

    return payload


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
        self.write_instrument_metadata_batch({symbol: fields})

    def write_instrument_metadata_batch(self, updates: dict[str, dict]) -> None:
        """Merge multiple listing patches in one atomic master-file write."""
        if not updates:
            return
        _validate_instrument_metadata(updates)
        all_meta = self.read_all_instrument_metadata()
        for symbol, fields in updates.items():
            all_meta[symbol] = {**all_meta.get(symbol, {}), **fields}
        self.replace_instrument_metadata(all_meta)

    def replace_instrument_metadata(self, metadata: dict[str, dict]) -> None:
        """Atomically replace the instrument master with validated records."""
        import json

        validated = _validate_instrument_metadata(metadata)
        path = self._instruments_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(validated, indent=2))
        tmp.replace(path)

    def read_instrument_metadata(self, symbol: str) -> dict | None:
        return self.read_all_instrument_metadata().get(symbol)

    def read_all_instrument_metadata(self) -> dict[str, dict]:
        import json

        path = self._instruments_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(_INSTRUMENT_METADATA_SHAPE_ERROR) from error
        return _validate_instrument_metadata(payload)

    # --- ISIN-addressed UCITS ETF profiles ---

    def _ucits_profile_path(self, isin: str) -> Path:
        from quantmind.instruments.metadata import normalize_isin

        return self.root / "ucits_profiles" / f"{normalize_isin(isin)}.json"

    def write_ucits_profile(self, profile: UcitsEtfProfileV1) -> None:
        """Atomically publish one validated UCITS share-class profile."""
        from quantmind.instruments.metadata import UcitsEtfProfileV1

        if not isinstance(profile, UcitsEtfProfileV1):
            raise TypeError("profile must be a UcitsEtfProfileV1")
        path = self._ucits_profile_path(profile.isin)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(profile.model_dump_json(indent=2))
        tmp.replace(path)

    def read_ucits_profile(self, isin: str) -> UcitsEtfProfileV1 | None:
        """Read and validate one UCITS profile; corrupt bytes are never served."""
        from quantmind.instruments.metadata import UcitsEtfProfileV1, normalize_isin

        requested_isin = normalize_isin(isin)
        path = self._ucits_profile_path(requested_isin)
        if not path.exists():
            return None
        try:
            profile = UcitsEtfProfileV1.model_validate_json(path.read_text())
            if profile.isin != requested_isin:
                raise ValueError("profile identity does not match its cache key")
            return profile
        except Exception as error:
            raise ValueError(f"corrupt UCITS profile for {path.stem}") from error
