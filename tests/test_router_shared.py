"""Tests for src/quantmind/api/routers/_shared.py — the pre-wave-3
consolidation pass (TODOS.md). `clean`/`iso`/`downsample`/`read_close_series`
are hand-checked directly; `weighted_portfolio_returns` is golden-tested
against hand-computed values that also match what whatif.py's
`position_returns @ weights_arr` and hedge.py's
`returns[symbols].to_numpy() @ weights_arr` computed before this extraction —
the full existing whatif/hedge endpoint test suites (beta-of-100%-SPY-book
~= 1, hedge sizing formula) are the end-to-end confirmation that the swap is
behavior-preserving.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from quantmind.api.routers._shared import (
    PositionIn,
    clean,
    collect_currency_assertions,
    downsample,
    iso,
    read_close_series,
    resolve_symbol_currencies,
    weighted_portfolio_returns,
)
from quantmind.datastore.store import BarStore


def test_collect_currency_assertions_normalizes_duplicate_symbol_claims():
    positions = [
        PositionIn(symbol="IWDA", qty=1, currency="eur"),
        PositionIn(symbol="IWDA", qty=2, currency=" EUR "),
        PositionIn(symbol="SPY", qty=1),
    ]

    assert collect_currency_assertions(positions) == {"IWDA": "EUR"}


def test_collect_currency_assertions_rejects_conflicting_duplicate_symbol_claims():
    positions = [
        PositionIn(symbol="IWDA", qty=1, currency="EUR"),
        PositionIn(symbol="IWDA", qty=2, currency="GBP"),
    ]

    with pytest.raises(HTTPException, match="conflicting currency assertions"):
        collect_currency_assertions(positions)


def test_currency_resolution_names_a_corrupt_instrument_master(tmp_path):
    store = BarStore(tmp_path)
    (tmp_path / "instruments.json").write_text('["not", "a", "mapping"]')

    with pytest.raises(HTTPException) as error:
        resolve_symbol_currencies(store, ["SPY"])

    assert error.value.status_code == 422
    assert "instrument metadata cache is corrupt" in error.value.detail


def test_currency_resolution_rejects_metadata_for_a_different_contract(tmp_path):
    store = BarStore(tmp_path)
    store.write_symbol_map({"IWDA": 2})
    store.write_instrument_metadata(
        "IWDA", {"con_id": 1, "currency": "USD", "provider": "ibkr"}
    )

    with pytest.raises(HTTPException) as error:
        resolve_symbol_currencies(store, ["IWDA"])

    assert error.value.status_code == 422
    assert "contract identity" in error.value.detail


def test_clean_passes_through_finite_numbers():
    assert clean(1.5) == 1.5
    assert clean(0) == 0.0
    assert clean(-3) == -3.0


def test_clean_nullifies_none_nan_inf():
    assert clean(None) is None
    assert clean(float("nan")) is None
    assert clean(float("inf")) is None
    assert clean(float("-inf")) is None


def test_clean_nullifies_non_numeric_without_raising():
    assert clean("not-a-number") is None  # type: ignore[arg-type]


def test_iso_formats_utc_z_suffixed():
    ts = pd.Timestamp("2026-07-24 00:00:00")
    assert iso(ts) == "2026-07-24T00:00:00Z"


def test_downsample_passthrough_under_max():
    seq = [1, 2, 3]
    assert downsample(seq, 10) is seq


def test_downsample_steps_lists_and_series_identically():
    lst = list(range(100))
    ser = pd.Series(range(100))
    lst_ds = downsample(lst, 10)
    ser_ds = downsample(ser, 10)
    assert len(lst_ds) <= 10
    assert list(ser_ds) == lst_ds


def test_position_in_rejects_zero_qty():
    with pytest.raises(Exception):
        PositionIn(symbol="SPY", qty=0)


def test_position_in_option_leg_fields_are_optional_and_default_none():
    p = PositionIn(symbol="SPY", qty=1)
    assert p.strike is None
    assert p.expiry is None
    assert p.right is None
    assert p.multiplier is None


def test_position_in_accepts_option_leg_fields():
    p = PositionIn(symbol="SPY", qty=1, strike=450.0, expiry="2026-09-18", right="C", multiplier=100.0)
    assert p.strike == 450.0
    assert p.right == "C"
    assert p.multiplier == 100.0


def test_read_close_series_missing_bars_is_422():
    class _EmptyStore:
        def read_bars(self, con_id, bar_size):
            raise FileNotFoundError

    with pytest.raises(HTTPException) as exc:
        read_close_series(_EmptyStore(), con_id=1, symbol="NOPE", years=1)
    assert exc.value.status_code == 422
    assert "NOPE" in exc.value.detail


def test_read_close_series_clips_to_years():
    idx = pd.bdate_range("2020-01-01", periods=1000)
    bars = pd.DataFrame({"close": np.arange(1000, dtype=float)}, index=idx)

    class _Store:
        def read_bars(self, con_id, bar_size):
            return bars, None

    series = read_close_series(_Store(), con_id=1, symbol="X", years=1)
    assert len(series) == 252
    assert series.iloc[-1] == 999.0


def test_weighted_portfolio_returns_hand_computed():
    # Two symbols, 3 aligned days; weights 0.6/0.4 (whatif-style per-position
    # weights that happen to equal per-symbol weights here).
    idx = pd.bdate_range("2026-01-01", periods=3)
    returns = pd.DataFrame({"A": [0.01, -0.02, 0.03], "B": [0.02, 0.00, -0.01]}, index=idx)
    weights = np.array([0.6, 0.4])
    out = weighted_portfolio_returns(returns, ["A", "B"], weights)
    expected = returns["A"] * 0.6 + returns["B"] * 0.4
    pd.testing.assert_series_equal(out, expected, check_names=False)


def test_weighted_portfolio_returns_repeated_symbol_reuses_column():
    # whatif.py's per-position call: two positions in the same symbol reuse
    # that symbol's column; their weights simply add.
    idx = pd.bdate_range("2026-01-01", periods=3)
    returns = pd.DataFrame({"A": [0.01, -0.02, 0.03]}, index=idx)
    weights = np.array([0.3, 0.3])
    out = weighted_portfolio_returns(returns, ["A", "A"], weights)
    expected = returns["A"] * 0.6
    pd.testing.assert_series_equal(out, expected, check_names=False)


def test_weighted_portfolio_returns_matches_manual_matrix_multiply():
    idx = pd.bdate_range("2026-01-01", periods=5)
    rng = np.random.default_rng(0)
    returns = pd.DataFrame(
        {"A": rng.normal(0, 0.01, 5), "B": rng.normal(0, 0.01, 5), "C": rng.normal(0, 0.01, 5)}, index=idx
    )
    symbols = ["A", "B", "C"]
    weights = np.array([0.5, 0.3, 0.2])
    out = weighted_portfolio_returns(returns, symbols, weights)
    manual = returns[symbols].to_numpy() @ weights
    assert np.allclose(out.to_numpy(), manual)
