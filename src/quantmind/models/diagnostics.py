"""Derived OU practitioner diagnostics (wave-3B Lab practitioner).

Pure, picklable functions — no I/O (Engineering Constraint 2). Everything the
Lab's half-life/displacement readout and random-walk gate need, testable
against hand-computed values:

* half_life_days: HL = ln2/θ (years) = ln2/(θ·dt) trading days, with a
  delta-method 95% CI propagated from the θ standard error
  (d(HL)/dθ = -ln2/θ² → se_HL = ln2·se_θ/(θ²·dt)). The lower bound floors at
  0 — a negative half-life is dimensionally absurd; a floor is the honest
  rendering of "the CI reaches the no-reversion boundary".
* stationary_sigma / displacement_sigma: the OU stationary distribution is
  N(μ, σ²/(2θ)); displacement is the current level's z-score in those
  stationary-σ units ("2.1σ above mean").
* rw_gate: fits the random-walk-with-drift null (Δx = c + ε, the b=1
  restriction of the OU AR(1) x' = a + b·x + ε) alongside the OU alternative
  and reports ΔAIC and the likelihood ratio. AICs are computed by hand as
  2k - 2·llf with k counting σ (k_ou=3: a, b, σ; k_rw=2: c, σ) so the two
  models are scored consistently — statsmodels' OLS aic omits σ from k.
  CAVEAT: under the unit-root null the LR statistic is NOT chi²(1)
  distributed (that's exactly the Dickey-Fuller problem), so no p-value is
  attached here; the caller composes ΔAIC with the ADF test (which has the
  correct unit-root critical values) to decide whether mean reversion is
  established. Compose, don't duplicate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

_Z95 = 1.96


def half_life_days(theta: float, theta_se: float, dt: float) -> tuple[float, tuple[float, float]] | None:
    """(half-life in trading days, 95% CI) for reversion speed `theta` (/yr)
    with standard error `theta_se`, at time step `dt` years/step. None when
    theta <= 0: no mean reversion, no half-life."""
    if theta <= 0 or dt <= 0:
        return None
    hl = math.log(2.0) / (theta * dt)
    se = math.log(2.0) * theta_se / (theta**2 * dt)
    return hl, (max(0.0, hl - _Z95 * se), hl + _Z95 * se)


def stationary_sigma(theta: float, sigma: float) -> float | None:
    """Standard deviation of the OU stationary distribution, σ/√(2θ).
    None when theta <= 0 (no stationary distribution exists)."""
    if theta <= 0:
        return None
    return sigma / math.sqrt(2.0 * theta)


def displacement_sigma(x_last: float, mu: float, theta: float, sigma: float) -> float | None:
    """Current displacement from the long-run mean in stationary-σ units:
    positive = above mean. None when the stationary distribution is undefined
    (theta <= 0) or degenerate (sigma == 0)."""
    sd = stationary_sigma(theta, sigma)
    if sd is None or sd == 0.0:
        return None
    return (x_last - mu) / sd


@dataclass(frozen=True)
class RwGate:
    aic_ou: float
    aic_rw: float
    delta_aic: float  # aic_rw - aic_ou: > 0 → OU favored, <= 0 → RW wins
    lr_stat: float  # 2·(llf_ou - llf_rw); no p-value (see module docstring)


def _gaussian_llf(resid: np.ndarray) -> float:
    """Concentrated Gaussian log-likelihood at the MLE variance."""
    n = resid.size
    s2 = float(np.mean(resid**2))
    if s2 <= 0.0:
        raise ValueError("degenerate (zero-variance) residuals; cannot score likelihood")
    return -0.5 * n * (math.log(2.0 * math.pi * s2) + 1.0)


def rw_gate(x: np.ndarray) -> RwGate:
    """Score the OU AR(1) alternative against the random-walk-with-drift null
    on the same observations. See module docstring for the AIC bookkeeping
    and the unit-root LR caveat."""
    x = np.asarray(x, dtype=float)
    if x.size < 4:
        raise ValueError(f"need >= 4 observations for the random-walk gate, got {x.size}")
    x_lag, x_next = x[:-1], x[1:]

    design = np.column_stack([np.ones(x_lag.size), x_lag])
    coef, *_ = np.linalg.lstsq(design, x_next, rcond=None)
    resid_ou = x_next - design @ coef
    llf_ou = _gaussian_llf(resid_ou)

    dx = np.diff(x)
    resid_rw = dx - dx.mean()
    llf_rw = _gaussian_llf(resid_rw)

    aic_ou = 2.0 * 3 - 2.0 * llf_ou
    aic_rw = 2.0 * 2 - 2.0 * llf_rw
    return RwGate(
        aic_ou=aic_ou,
        aic_rw=aic_rw,
        delta_aic=aic_rw - aic_ou,
        lr_stat=2.0 * (llf_ou - llf_rw),
    )
