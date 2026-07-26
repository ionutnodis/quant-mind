"""Golden-value tests for the VIX volatility-regime tagger."""

import numpy as np
import pandas as pd
import pytest

from quantmind.analytics.regime import regime_tag
from quantmind.risk.returns import InsufficientDataError


def _vix(vals):
    return pd.Series(vals, index=pd.bdate_range("2026-01-02", periods=len(vals)), dtype=float)


def test_regime_tag_terciles_label_low_med_high():
    # 9 values; default terciles at the 1/3 and 2/3 quantiles (pandas linear).
    # sorted: 10,12,14,16,18,20,40,50,60
    # q(1/3): h=(9-1)/3=2.667 -> 14 + .667*(16-14) = 15.333
    # q(2/3): h=(9-1)*2/3=5.333 -> 20 + .333*(40-20) = 26.667
    # <=15.333 -> low ; <=26.667 -> med ; else high  => clean 3/3/3 split
    vix = _vix([10, 12, 14, 16, 18, 20, 40, 50, 60])
    labels = regime_tag(vix)
    assert list(labels) == [
        "low", "low", "low", "med", "med", "med", "high", "high", "high"
    ]
    assert list(labels.index) == list(vix.index)


def test_regime_tag_drops_nan_without_mislabeling():
    vix = _vix([10, np.nan, 14, 16, 18, 20, 40, 50, 60])
    labels = regime_tag(vix)
    # the NaN row is not labeled (dropped), finite rows keep honest labels
    assert labels.isna().sum() == 0
    assert "med" in set(labels) and "high" in set(labels) and "low" in set(labels)
    assert pd.Timestamp("2026-01-03") not in labels.index  # the NaN date dropped


def test_regime_tag_requires_three_observations():
    with pytest.raises(InsufficientDataError):
        regime_tag(_vix([15, 25]))


def test_regime_tag_rejects_bad_quantiles():
    with pytest.raises(ValueError):
        regime_tag(_vix([10, 20, 30, 40]), low_q=0.7, high_q=0.3)
    with pytest.raises(ValueError):
        regime_tag(_vix([10, 20, 30, 40]), low_q=0.0, high_q=0.5)
