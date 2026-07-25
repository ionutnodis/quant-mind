"""Exposure bridge: the shock contract. Wrong units must be an error, never a wrong number."""

import numpy as np
import pytest

from quantmind.exposure.bridge import Exposure, UnsupportedMappingError, apply_to_book
from quantmind.models.base import Factor

RATE_FACTOR = Factor(kind="rate_level", units="decimal", dt=1 / 252)


def test_duration_hand_case():
    # Book: -$610 per bp. Rates go from 4.18% to 4.28% (+10bp) -> P&L = -$6,100.
    paths = np.array([[0.0420, 0.0428]])  # one path, terminal 4.28%
    exposure = Exposure(factor_kind="rate_level", units="usd_per_bp", value=-610.0)
    pnl = apply_to_book(paths, initial=0.0418, factor=RATE_FACTOR, exposure=exposure)
    assert pnl.shape == (1,)
    assert pnl[0] == pytest.approx(-610.0 * 10.0)


def test_distribution_over_paths():
    paths = np.array([[0.0418], [0.0428], [0.0408]])  # flat, +10bp, -10bp
    exposure = Exposure(factor_kind="rate_level", units="usd_per_bp", value=-610.0)
    pnl = apply_to_book(paths, initial=0.0418, factor=RATE_FACTOR, exposure=exposure)
    assert pnl[0] == pytest.approx(0.0)
    assert pnl[1] == pytest.approx(-6100.0)
    assert pnl[2] == pytest.approx(+6100.0)


def test_unsupported_mapping_is_explicit_error_not_wrong_number():
    paths = np.array([[0.05]])
    vega_exposure = Exposure(factor_kind="vol_points", units="usd_per_volpt", value=-184.0)
    with pytest.raises(UnsupportedMappingError, match="rate_level"):
        apply_to_book(paths, initial=0.0418, factor=RATE_FACTOR, exposure=vega_exposure)


def test_unknown_unit_conversion_is_explicit_error():
    paths = np.array([[0.05]])
    weird = Exposure(factor_kind="rate_level", units="usd_per_furlong", value=1.0)
    with pytest.raises(UnsupportedMappingError, match="furlong"):
        apply_to_book(paths, initial=0.0418, factor=RATE_FACTOR, exposure=weird)
