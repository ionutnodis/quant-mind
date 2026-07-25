"""Model contract (Phase Plan: model registry).

Every model declares WHAT it simulates via a typed Factor — kind, units, dt —
so the exposure bridge can validate compatibility instead of multiplying
mismatched dimensions. FitResult carries the full mathematical transparency the
Lab UI displays: estimates, confidence intervals, diagnostics.

    data ──> fit ──> FitResult(params, cis, diagnostics)
                        │
                        ▼
             simulate(fit, horizon, n_paths, seed) ──> paths (n_paths × horizon)
                        │
                        ▼  (exposure bridge validates Factor vs Exposure)
             apply_to_book ──> P&L distribution
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Factor:
    kind: str  # "rate_level" | "equity_return" | "vol_points"
    units: str  # e.g. "decimal" for rate levels
    dt: float  # time step in years (daily = 1/252)
    reference: float | None = None


@dataclass(frozen=True)
class FitResult:
    model_name: str
    params: dict[str, float]
    cis: dict[str, tuple[float, float]]  # 95% confidence intervals
    diagnostics: dict[str, float]
    n_obs: int


class Model(Protocol):  # pragma: no cover - structural typing only
    name: str
    factor: Factor

    def param_schema(self) -> dict: ...

    def fit(self, series: pd.Series) -> FitResult: ...

    def simulate(
        self, fit: FitResult, horizon: int, n_paths: int, seed: int | None, x0: float
    ) -> np.ndarray: ...
