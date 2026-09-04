import pytest

from quantmind.risk.options import OptionLeg, aggregate_greeks, bs_greeks, bs_price, stress_grid


def test_bs_price_matches_textbook_value():
    # S=100, K=100, T=1y, r=0, sigma=20% -> European call ~= 7.9656
    assert bs_price(spot=100, strike=100, t=1.0, r=0.0, sigma=0.2, is_call=True) == pytest.approx(
        7.9656, abs=1e-3
    )


@pytest.mark.parametrize("sigma", [0.0, -0.01])
def test_bs_price_rejects_non_positive_volatility(sigma):
    with pytest.raises(ValueError, match="volatility"):
        bs_price(spot=100, strike=100, t=1.0, r=0.0, sigma=sigma, is_call=True)


def test_put_call_parity():
    c = bs_price(spot=105, strike=100, t=0.5, r=0.03, sigma=0.25, is_call=True)
    p = bs_price(spot=105, strike=100, t=0.5, r=0.03, sigma=0.25, is_call=False)
    import math

    assert c - p == pytest.approx(105 - 100 * math.exp(-0.03 * 0.5), abs=1e-9)


def test_atm_call_delta_known_value():
    g = bs_greeks(spot=100, strike=100, t=1.0, r=0.0, sigma=0.2, is_call=True)
    assert g.delta == pytest.approx(0.5398, abs=1e-3)


def test_aggregate_greeks_two_leg_hand_computed():
    spot, r = 100.0, 0.0
    call = OptionLeg(qty=2, strike=105, expiry_years=0.25, is_call=True, iv=0.3)
    put = OptionLeg(qty=-1, strike=95, expiry_years=0.25, is_call=False, iv=0.35)
    gc = bs_greeks(spot, call.strike, call.expiry_years, r, call.iv, True)
    gp = bs_greeks(spot, put.strike, put.expiry_years, r, put.iv, False)

    total = aggregate_greeks([call, put], spot=spot, r=r, shares=100)
    # shares contribute delta 1 each, zero gamma/vega/theta
    assert total.delta == pytest.approx(100 + 2 * 100 * gc.delta - 1 * 100 * gp.delta)
    assert total.gamma == pytest.approx(2 * 100 * gc.gamma - 1 * 100 * gp.gamma)
    assert total.vega == pytest.approx(2 * 100 * gc.vega - 1 * 100 * gp.vega)


def test_stress_grid_zero_shock_cell_is_zero_pnl():
    leg = OptionLeg(qty=1, strike=100, expiry_years=0.5, is_call=True, iv=0.25)
    grid = stress_grid([leg], spot=100, r=0.0, spot_shocks=(-0.1, 0.0, 0.1), vol_shocks=(-0.05, 0.0, 0.05))
    assert grid.loc[0.0, 0.0] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("iv", [0.03, 0.05])
def test_stress_grid_floors_low_iv_after_negative_vol_shock(iv):
    leg = OptionLeg(qty=1, strike=100, expiry_years=0.5, is_call=True, iv=iv)

    grid = stress_grid(
        [leg],
        spot=100,
        r=0.0,
        spot_shocks=(-0.1, 0.0, 0.1),
        vol_shocks=(-0.05, 0.0, 0.05),
    )

    assert all(value == value and abs(value) != float("inf") for value in grid.to_numpy().flat)


def test_stress_grid_long_call_direction_and_vega_sign():
    leg = OptionLeg(qty=1, strike=100, expiry_years=0.5, is_call=True, iv=0.25)
    grid = stress_grid([leg], spot=100, r=0.0, spot_shocks=(-0.1, 0.0, 0.1), vol_shocks=(0.0, 0.10))
    assert grid.loc[0.0, 0.1] > 0  # spot up -> long call gains
    assert grid.loc[0.0, -0.1] < 0  # spot down -> long call loses
    assert grid.loc[0.10, 0.0] > 0  # vol up -> long option gains


def test_stress_grid_includes_share_position():
    grid = stress_grid([], spot=50, r=0.0, shares=100, spot_shocks=(-0.2, 0.0, 0.2), vol_shocks=(0.0,))
    assert grid.loc[0.0, 0.2] == pytest.approx(100 * 50 * 0.2)
    assert grid.loc[0.0, -0.2] == pytest.approx(-100 * 50 * 0.2)
