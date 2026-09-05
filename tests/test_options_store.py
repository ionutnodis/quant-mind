"""OptionsStore: options chain snapshots persisted as parquet, one file per
underlier (root/options/{underlier}.parquet), atomic tmp-replace writes
(pattern: datastore/store.py's BarStore) — a full-chain re-snapshot REPLACES
the file (strikes/expiries shift as spot moves and time passes; there is no
append-only history requirement for the options sleeve, unlike adjusted bars).
as_of + spot travel in the parquet schema metadata, same technique as
BarStore's BarMeta.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pyarrow.parquet as pq
import pytest

from quantmind.datastore.options_store import (
    OptionsSnapshotMeta,
    OptionsStore,
    option_chain_freshness,
)


def _chain_df():
    return pd.DataFrame(
        {
            "expiry": ["20260918", "20260918", "20260918", "20260918"],
            "strike": [440.0, 440.0, 450.0, 450.0],
            "right": ["C", "P", "C", "P"],
            "con_id": [1001, 1002, 1003, 1004],
            "bid": [10.1, 8.2, 5.5, 12.3],
            "ask": [10.3, 8.4, 5.7, 12.5],
            "iv": [0.18, 0.20, 0.19, 0.21],
            "delta": [0.55, -0.45, 0.40, -0.60],
            "multiplier": [100.0, 100.0, 100.0, 100.0],
        }
    )


def test_write_then_read_round_trips_chain_and_meta(tmp_path):
    store = OptionsStore(tmp_path)
    meta = OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=756733)
    store.write_chain("SPY", _chain_df(), meta)

    df, read_meta = store.read_chain("SPY")
    pd.testing.assert_frame_equal(df.reset_index(drop=True), _chain_df())
    assert read_meta.as_of == "2026-07-25"
    assert read_meta.spot == pytest.approx(452.10)
    assert read_meta.underlier_con_id == 756733


def test_option_chain_freshness_counts_business_days_across_a_weekend():
    age_days, stale = option_chain_freshness("2026-03-27", date(2026, 3, 31))

    assert age_days == 2
    assert stale is False


def test_read_missing_underlier_raises_file_not_found(tmp_path):
    store = OptionsStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_chain("SPY")


def test_read_rejects_chain_with_missing_required_columns(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df().drop(columns=["con_id"]),
        OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=1),
    )

    with pytest.raises(ValueError, match="missing columns"):
        store.read_chain("SPY")


def test_read_rejects_legacy_chain_without_underlier_contract_identity(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=1),
    )
    path = tmp_path / "options" / "SPY.parquet"
    table = pq.read_table(path)
    metadata = {
        key: value
        for key, value in (table.schema.metadata or {}).items()
        if key != b"quantmind.options.underlier_con_id"
    }
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="missing metadata"):
        store.read_chain("SPY")


def test_has_chain_reflects_presence(tmp_path):
    store = OptionsStore(tmp_path)
    assert store.has_chain("SPY") is False
    store.write_chain(
        "SPY",
        _chain_df(),
        OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=1),
    )
    assert store.has_chain("SPY") is True
    assert store.has_chain("QQQ") is False


def test_write_replaces_prior_snapshot_entirely(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=1),
    )
    smaller = _chain_df().iloc[:1]
    store.write_chain(
        "SPY",
        smaller,
        OptionsSnapshotMeta(as_of="2026-07-26", spot=455.00, underlier_con_id=1),
    )

    df, meta = store.read_chain("SPY")
    assert len(df) == 1
    assert meta.as_of == "2026-07-26"
    assert meta.spot == pytest.approx(455.00)


def test_chains_are_isolated_per_underlier(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10, underlier_con_id=1),
    )
    store.write_chain(
        "QQQ",
        _chain_df().iloc[:2],
        OptionsSnapshotMeta(as_of="2026-07-25", spot=380.0, underlier_con_id=2),
    )

    spy_df, _ = store.read_chain("SPY")
    qqq_df, _ = store.read_chain("QQQ")
    assert len(spy_df) == 4
    assert len(qqq_df) == 2
