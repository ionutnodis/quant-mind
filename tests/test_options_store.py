"""OptionsStore: options chain snapshots persisted as parquet, one file per
underlier (root/options/{underlier}.parquet), atomic tmp-replace writes
(pattern: datastore/store.py's BarStore) — a full-chain re-snapshot REPLACES
the file (strikes/expiries shift as spot moves and time passes; there is no
append-only history requirement for the options sleeve, unlike adjusted bars).
as_of + spot travel in the parquet schema metadata, same technique as
BarStore's BarMeta.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore


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
    meta = OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10)
    store.write_chain("SPY", _chain_df(), meta)

    df, read_meta = store.read_chain("SPY")
    pd.testing.assert_frame_equal(df.reset_index(drop=True), _chain_df())
    assert read_meta.as_of == "2026-07-25"
    assert read_meta.spot == pytest.approx(452.10)


def test_read_missing_underlier_raises_file_not_found(tmp_path):
    store = OptionsStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_chain("SPY")


def test_has_chain_reflects_presence(tmp_path):
    store = OptionsStore(tmp_path)
    assert store.has_chain("SPY") is False
    store.write_chain("SPY", _chain_df(), OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10))
    assert store.has_chain("SPY") is True
    assert store.has_chain("QQQ") is False


def test_write_replaces_prior_snapshot_entirely(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain("SPY", _chain_df(), OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10))
    smaller = _chain_df().iloc[:1]
    store.write_chain("SPY", smaller, OptionsSnapshotMeta(as_of="2026-07-26", spot=455.00))

    df, meta = store.read_chain("SPY")
    assert len(df) == 1
    assert meta.as_of == "2026-07-26"
    assert meta.spot == pytest.approx(455.00)


def test_chains_are_isolated_per_underlier(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain("SPY", _chain_df(), OptionsSnapshotMeta(as_of="2026-07-25", spot=452.10))
    store.write_chain("QQQ", _chain_df().iloc[:2], OptionsSnapshotMeta(as_of="2026-07-25", spot=380.0))

    spy_df, _ = store.read_chain("SPY")
    qqq_df, _ = store.read_chain("QQQ")
    assert len(spy_df) == 4
    assert len(qqq_df) == 2
