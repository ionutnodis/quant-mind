"""The shock contract (Phase Plan): factor paths -> book P&L, with unit
validation. An unsupported model->book mapping raises, never mis-multiplies.

Linear approximation: P&L = exposure x (terminal - initial), converted to the
exposure's units. Full repricing on paths is the v2 unified-ES roadmap; the UI
labels this approximation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from quantmind.models.base import Factor


class UnsupportedMappingError(ValueError):
    pass


@dataclass(frozen=True)
class Exposure:
    factor_kind: str  # must match the model's Factor.kind
    units: str  # "usd_per_bp" | "usd_per_volpt" | "usd_per_return"
    value: float


# (factor units, exposure units) -> multiplier converting a factor delta into exposure units
_CONVERSIONS = {
    ("decimal", "usd_per_bp"): 1e4,  # decimal rate delta -> basis points
    ("return", "usd_per_return"): 1.0,
    ("vol_points", "usd_per_volpt"): 1.0,
}


def apply_to_book(
    paths: np.ndarray, initial: float, factor: Factor, exposure: Exposure
) -> np.ndarray:
    """P&L per path from terminal factor moves. Shape: (n_paths,)."""
    if exposure.factor_kind != factor.kind:
        raise UnsupportedMappingError(
            f"exposure is for factor kind {exposure.factor_kind!r} but model simulates "
            f"{factor.kind!r} — refusing to produce a dimensionally wrong number"
        )
    key = (factor.units, exposure.units)
    if key not in _CONVERSIONS:
        raise UnsupportedMappingError(
            f"no unit conversion from factor units {factor.units!r} to exposure units "
            f"{exposure.units!r}"
        )
    delta = (paths[:, -1] - initial) * _CONVERSIONS[key]
    return exposure.value * delta
