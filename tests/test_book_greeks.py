"""book_greeks.py: pure composition over the tested risk/options.py core
(aggregate_greeks, stress_grid) — per-underlying net Greeks, dollar-delta,
SPY-equivalent notional (per-underlier beta passed in), and a book-level
option-sleeve stress grid. Hand-computed 2-leg cases, same style as
tests/test_options_risk.py (this module must never re-derive Black-Scholes
math itself — it only groups/sums/scales the already-tested primitives).
"""

from __future__ import annotations

import pytest

from quantmind.exposure.book_greeks import (
    BookLeg,
    aggregate_book_stress_grid,
    compute_book_greeks,
)
from quantmind.risk.options import bs_greeks


def test_single_underlier_two_option_legs_plus_shares_matches_hand_computation():
    spot, r = 100.0, 0.0
    call = BookLeg(
        underlier="SPY", qty=2, is_option=True, spot=spot, r=r,
        strike=105, expiry_years=0.25, is_call=True, iv=0.3, multiplier=100.0,
    )
    put = BookLeg(
        underlier="SPY", qty=-1, is_option=True, spot=spot, r=r,
        strike=95, expiry_years=0.25, is_call=False, iv=0.35, multiplier=100.0,
    )
    shares = BookLeg(underlier="SPY", qty=100, is_option=False, spot=spot, r=r)

    gc = bs_greeks(spot, call.strike, call.expiry_years, r, call.iv, True)
    gp = bs_greeks(spot, put.strike, put.expiry_years, r, put.iv, False)
    expected_delta = 100 + 2 * 100 * gc.delta - 1 * 100 * gp.delta
    expected_gamma = 2 * 100 * gc.gamma - 1 * 100 * gp.gamma
    expected_vega = 2 * 100 * gc.vega - 1 * 100 * gp.vega
    expected_theta = 2 * 100 * gc.theta - 1 * 100 * gp.theta

    [result] = compute_book_greeks([call, put, shares])
    assert result.underlier == "SPY"
    assert result.spot == pytest.approx(spot)
    assert result.delta == pytest.approx(expected_delta)
    assert result.gamma == pytest.approx(expected_gamma)
    assert result.vega == pytest.approx(expected_vega)
    assert result.theta == pytest.approx(expected_theta)
    # dollar_delta = delta (shares-equivalent) * spot: an actual dollar
    # notional of directional exposure, not a per-$1-move sensitivity.
    assert result.dollar_delta == pytest.approx(expected_delta * spot)
    assert result.spy_equivalent_notional is None  # no beta supplied


def test_spy_equivalent_notional_scales_dollar_delta_by_beta():
    leg = BookLeg(underlier="QQQ", qty=1, is_option=False, spot=380.0, r=0.0)
    [result] = compute_book_greeks([leg], betas={"QQQ": 1.1})
    assert result.dollar_delta == pytest.approx(380.0)  # 1 share -> delta 1 -> $spot
    assert result.spy_equivalent_notional == pytest.approx(380.0 * 1.1)


def test_missing_beta_for_underlier_leaves_notional_none():
    leg = BookLeg(underlier="IWM", qty=10, is_option=False, spot=200.0, r=0.0)
    [result] = compute_book_greeks([leg], betas={"QQQ": 1.1})  # no IWM entry
    assert result.spy_equivalent_notional is None


def test_book_greeks_groups_by_underlier_independently():
    spy_leg = BookLeg(underlier="SPY", qty=100, is_option=False, spot=450.0, r=0.0)
    qqq_leg = BookLeg(underlier="QQQ", qty=-50, is_option=False, spot=380.0, r=0.0)
    results = {r.underlier: r for r in compute_book_greeks([spy_leg, qqq_leg])}
    assert set(results) == {"SPY", "QQQ"}
    assert results["SPY"].delta == pytest.approx(100.0)
    assert results["QQQ"].delta == pytest.approx(-50.0)


def test_aggregate_book_stress_grid_sums_per_underlier_grids_at_zero_shock():
    # Two independent single-share positions: at the zero/zero cell every
    # per-underlier grid is 0 P&L by construction (risk/options.py's own
    # invariant, test_stress_grid_zero_shock_cell_is_zero_pnl) so the summed
    # book grid must also be exactly zero there.
    spy_leg = BookLeg(underlier="SPY", qty=100, is_option=False, spot=450.0, r=0.0)
    qqq_leg = BookLeg(underlier="QQQ", qty=50, is_option=False, spot=380.0, r=0.0)
    grid = aggregate_book_stress_grid(
        [spy_leg, qqq_leg], spot_shocks=(-0.1, 0.0, 0.1), vol_shocks=(0.0,)
    )
    assert grid.loc[0.0, 0.0] == pytest.approx(0.0, abs=1e-9)
    # +10% spot shock: SPY leg gains 100*450*0.1, QQQ leg gains 50*380*0.1
    assert grid.loc[0.0, 0.1] == pytest.approx(100 * 450 * 0.1 + 50 * 380 * 0.1)


def test_aggregate_book_stress_grid_empty_book_returns_empty_frame():
    grid = aggregate_book_stress_grid([], spot_shocks=(-0.1, 0.0, 0.1), vol_shocks=(0.0,))
    assert list(grid.index) == [0.0]
    assert (grid.loc[0.0] == 0.0).all()
