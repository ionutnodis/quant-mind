"""Golden tests for the core-vs-overlay P&L attribution module (Task B1):
daily book P&L decomposed into beta*bench_return*book_value (core) vs the
residual (overlay). Pure — pandas in, pandas/dataclass out. Hand-computed
values, same style as tests/test_book_greeks.py.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantmind.exposure.attribution import (
    InsufficientDataError,
    decompose_book_pnl,
    summarize_pnl_split,
)


def _series(values: list[float]) -> pd.Series:
    idx = pd.bdate_range("2026-07-01", periods=len(values))
    return pd.Series(values, index=idx)


def test_decompose_book_pnl_matches_hand_computed_values():
    book_returns = _series([0.02, -0.01, 0.03])
    bench_returns = _series([0.01, -0.02, 0.02])
    beta = 1.5
    book_value = 10_000.0

    out = decompose_book_pnl(book_returns, bench_returns, beta=beta, book_value=book_value)

    assert list(out.columns) == ["total_pnl", "core_pnl", "overlay_pnl"]
    assert out["total_pnl"].tolist() == pytest.approx([200.0, -100.0, 300.0])
    assert out["core_pnl"].tolist() == pytest.approx([150.0, -300.0, 300.0])
    assert out["overlay_pnl"].tolist() == pytest.approx([50.0, 200.0, 0.0])
    # total = core + overlay, exactly, at every observation
    assert (out["total_pnl"] - (out["core_pnl"] + out["overlay_pnl"])).abs().max() < 1e-9


def test_decompose_book_pnl_aligns_on_overlapping_dates_only():
    book_idx = pd.bdate_range("2026-07-01", periods=3)
    bench_idx = pd.bdate_range("2026-07-02", periods=3)  # shifted by one day
    book_returns = pd.Series([0.01, 0.02, 0.03], index=book_idx)
    bench_returns = pd.Series([0.01, 0.02, 0.03], index=bench_idx)

    out = decompose_book_pnl(book_returns, bench_returns, beta=1.0, book_value=1_000.0)
    # only 2 overlapping business days
    assert len(out) == 2


def test_decompose_book_pnl_raises_on_no_overlap():
    book_returns = _series([0.01])
    bench_returns = pd.Series([0.01], index=pd.bdate_range("2020-01-01", periods=1))
    with pytest.raises(InsufficientDataError):
        decompose_book_pnl(book_returns, bench_returns, beta=1.0, book_value=1_000.0)


def test_summarize_pnl_split_matches_hand_computed_sums_and_shares():
    book_returns = _series([0.02, -0.01, 0.03])
    bench_returns = _series([0.01, -0.02, 0.02])
    decomposed = decompose_book_pnl(book_returns, bench_returns, beta=1.5, book_value=10_000.0)

    summary = summarize_pnl_split(decomposed)

    assert summary.n_obs == 3
    assert summary.total_pnl == pytest.approx(400.0)
    assert summary.core_pnl == pytest.approx(150.0)
    assert summary.overlay_pnl == pytest.approx(250.0)
    assert summary.core_share == pytest.approx(150.0 / 400.0)
    assert summary.overlay_share == pytest.approx(250.0 / 400.0)


def test_summarize_pnl_split_zero_total_pnl_leaves_shares_none():
    # core and overlay cancel exactly -> total_pnl 0.0, shares undefined (never a div-by-zero NaN)
    book_returns = _series([0.0])
    bench_returns = _series([0.0])
    decomposed = decompose_book_pnl(book_returns, bench_returns, beta=1.0, book_value=1_000.0)

    summary = summarize_pnl_split(decomposed)
    assert summary.total_pnl == pytest.approx(0.0)
    assert summary.core_share is None
    assert summary.overlay_share is None


def test_summarize_pnl_split_raises_on_empty_frame():
    empty = pd.DataFrame({"total_pnl": [], "core_pnl": [], "overlay_pnl": []})
    with pytest.raises(InsufficientDataError):
        summarize_pnl_split(empty)
