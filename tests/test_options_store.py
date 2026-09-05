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
            "observed_at": ["2026-07-25T15:30:00Z"] * 4,
            "market_data_type": [1] * 4,
        }
    )


def _meta(
    *,
    as_of: str = "2026-07-25T15:30:00Z",
    spot: float = 452.10,
    underlier_con_id: int = 1,
) -> OptionsSnapshotMeta:
    return OptionsSnapshotMeta(
        as_of=as_of,
        spot=spot,
        underlier_con_id=underlier_con_id,
    )


def test_write_then_read_round_trips_chain_and_meta(tmp_path):
    store = OptionsStore(tmp_path)
    meta = _meta(underlier_con_id=756733)
    store.write_chain("SPY", _chain_df(), meta)

    df, read_meta = store.read_chain("SPY")
    pd.testing.assert_frame_equal(df.reset_index(drop=True), _chain_df())
    assert read_meta.as_of == "2026-07-25T15:30:00Z"
    assert read_meta.spot == pytest.approx(452.10)
    assert read_meta.underlier_con_id == 756733


def test_option_chain_freshness_counts_business_days_across_a_weekend():
    age_days, stale = option_chain_freshness("2026-03-27", date(2026, 3, 31))

    assert age_days == 2
    assert stale is False


def test_option_chain_freshness_rejects_future_dated_evidence():
    age_days, stale = option_chain_freshness("2026-04-01", date(2026, 3, 31))

    assert age_days is None
    assert stale is True


def test_read_missing_underlier_raises_file_not_found(tmp_path):
    store = OptionsStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_chain("SPY")


def test_read_rejects_chain_with_missing_required_columns(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        _meta(),
    )
    path = tmp_path / "options" / "SPY.parquet"
    table = pq.read_table(path)
    pq.write_table(table.drop(["con_id"]), path)

    with pytest.raises(ValueError, match="missing columns"):
        store.read_chain("SPY")


@pytest.mark.parametrize("column", ["observed_at", "market_data_type"])
def test_read_rejects_legacy_chain_without_per_quote_evidence(tmp_path, column):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        _meta(),
    )
    path = tmp_path / "options" / "SPY.parquet"
    table = pq.read_table(path)
    pq.write_table(table.drop([column]), path)

    with pytest.raises(ValueError, match="missing columns"):
        store.read_chain("SPY")


def test_write_rejects_quote_timestamp_without_timezone(tmp_path):
    store = OptionsStore(tmp_path)
    frame = _chain_df().assign(observed_at="2026-07-25T15:30:00")

    with pytest.raises(ValueError, match="timezone-aware"):
        store.write_chain(
            "SPY",
            frame,
            _meta(),
        )


@pytest.mark.parametrize("market_data_type", [None, 0, 5, 1.5, True])
def test_write_rejects_invalid_quote_market_data_type(tmp_path, market_data_type):
    store = OptionsStore(tmp_path)
    frame = _chain_df().assign(market_data_type=market_data_type)

    with pytest.raises(ValueError, match="market_data_type"):
        store.write_chain(
            "SPY",
            frame,
            _meta(),
        )


def test_write_allows_later_quote_observations_when_snapshot_is_the_minimum(tmp_path):
    store = OptionsStore(tmp_path)
    frame = _chain_df()
    frame.loc[1:, "observed_at"] = "2026-07-25T16:00:01Z"

    store.write_chain("SPY", frame, _meta())

    stored, meta = store.read_chain("SPY")
    assert meta.as_of == "2026-07-25T15:30:00Z"
    assert stored["observed_at"].max() == "2026-07-25T16:00:01Z"


@pytest.mark.parametrize(
    "as_of",
    ["2026-07-25T15:29:59Z", "2026-07-25T15:30:01Z"],
)
def test_write_rejects_snapshot_that_is_not_weakest_quote_observation(tmp_path, as_of):
    store = OptionsStore(tmp_path)

    with pytest.raises(ValueError, match="weakest quote observation"):
        store.write_chain("SPY", _chain_df(), _meta(as_of=as_of))


def test_read_rejects_legacy_chain_without_underlier_contract_identity(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        _meta(),
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
        _meta(),
    )
    assert store.has_chain("SPY") is True
    assert store.has_chain("QQQ") is False


def test_write_replaces_prior_snapshot_entirely(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        _meta(),
    )
    smaller = _chain_df().iloc[:1]
    store.write_chain(
        "SPY",
        smaller,
        _meta(as_of="2026-07-25T15:30:00Z", spot=455.00),
    )

    df, meta = store.read_chain("SPY")
    assert len(df) == 1
    assert meta.as_of == "2026-07-25T15:30:00Z"
    assert meta.spot == pytest.approx(455.00)


def test_chains_are_isolated_per_underlier(tmp_path):
    store = OptionsStore(tmp_path)
    store.write_chain(
        "SPY",
        _chain_df(),
        _meta(),
    )
    store.write_chain(
        "QQQ",
        _chain_df().iloc[:2],
        _meta(spot=380.0, underlier_con_id=2),
    )

    spy_df, _ = store.read_chain("SPY")
    qqq_df, _ = store.read_chain("QQQ")
    assert len(spy_df) == 4
    assert len(qqq_df) == 2
