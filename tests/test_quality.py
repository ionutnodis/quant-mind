import numpy as np
import pandas as pd
import pytest

from quantmind.datastore.quality import align_calendars, quality_report


def _series(dates, values):
    return pd.Series(values, index=pd.DatetimeIndex(dates))


def test_quality_report_finds_max_nan_run():
    s = _series(
        pd.bdate_range("2026-01-05", periods=8),
        [1.0, np.nan, np.nan, np.nan, 1.0, np.nan, 1.0, 1.0],
    )
    rep = quality_report(s)
    assert rep.nan_run_max == 3
    assert not rep.ok


def test_quality_report_counts_interior_missing_business_days():
    dates = list(pd.bdate_range("2026-01-05", periods=10))
    del dates[3:5]  # drop two interior business days entirely
    s = _series(dates, [1.0] * 8)
    rep = quality_report(s)
    assert rep.n_missing_days == 2


def test_quality_report_counts_zero_volume_bars():
    idx = pd.bdate_range("2026-01-05", periods=4)
    prices = _series(idx, [1.0, 1.0, 1.0, 1.0])
    volume = _series(idx, [100, 0, 0, 50])
    rep = quality_report(prices, volume=volume)
    assert rep.n_zero_volume == 2


def test_clean_series_is_ok():
    s = _series(pd.bdate_range("2026-01-05", periods=5), [1.0, 2.0, 3.0, 4.0, 5.0])
    assert quality_report(s).ok


def test_align_calendars_union_index_with_fill_mask():
    # US series missing Jan 7 (US holiday), EU series missing Jan 8 (EU holiday)
    us = _series(["2026-01-05", "2026-01-06", "2026-01-08"], [10.0, 11.0, 12.0])
    eu = _series(["2026-01-05", "2026-01-07", "2026-01-08"], [20.0, 21.0, 22.0])
    aligned, filled = align_calendars({"us": us, "eu": eu})
    assert list(aligned.index) == list(pd.DatetimeIndex(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]))
    # gaps forward-filled but FLAGGED, never silent
    assert aligned.loc["2026-01-07", "us"] == 11.0
    assert filled.loc["2026-01-07", "us"]
    assert aligned.loc["2026-01-06", "eu"] == 20.0
    assert filled.loc["2026-01-06", "eu"]
    assert not filled.loc["2026-01-05", "us"]


def test_align_calendars_preserves_leading_gap_as_nan():
    a = _series(["2026-01-06", "2026-01-07"], [1.0, 2.0])
    b = _series(["2026-01-05", "2026-01-06", "2026-01-07"], [5.0, 6.0, 7.0])
    aligned, filled = align_calendars({"a": a, "b": b})
    assert np.isnan(aligned.loc["2026-01-05", "a"])  # no fabricated history before first obs
    assert not filled.loc["2026-01-05", "a"]
