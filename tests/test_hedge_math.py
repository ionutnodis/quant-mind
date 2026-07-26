"""Golden tests for the wave-3B "Hedge honest" pure math (src/quantmind/hedge/):

- cost.py: carry drag (beta_h * E[r_bench], annualized), borrow proxy for
  short/inverse candidates (labeled config-style constant), protection-per-cost.
- bootstrap.py: paired seeded block-bootstrap CI on delta-ES (mirrors
  risk/montecarlo.py's all-randomness-up-front block sampling).
- tail.py: tail-conditional protection — book P&L with vs without a hedge on
  the worst-decile benchmark days.
- option_hedges.py: protective put / put spread / collar built from a cached
  chain, sized off risk/options.py's stress_grid, premium as % annual drag.

Every number below is hand-computed (or, where a Black-Scholes price is a
factor, composed from the already-golden-tested risk.options.bs_price — the
assertion then checks OUR composition, not the pricer).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantmind.hedge.bootstrap import delta_es_ci
from quantmind.hedge.cost import (
    BORROW_PROXY_RATE,
    annualized_mean_return,
    borrow_proxy_annual,
    carry_drag_annual,
    protection_per_cost,
)
from quantmind.hedge.option_hedges import (
    build_structures,
    premium_annual_drag,
    size_contracts,
    structure_daily_pnl,
)
from quantmind.hedge.tail import worst_decile_tail
from quantmind.risk.options import OptionLeg, bs_price, stress_grid
from quantmind.risk.returns import historical_es


# --- cost.py ---


def test_annualized_mean_return_hand_computed():
    r = pd.Series([0.01, -0.005, 0.02])
    # mean = 0.025/3 = 0.008333...; * 252 = 2.1
    assert annualized_mean_return(r) == pytest.approx(2.1)


def test_carry_drag_short_positive_beta_is_positive_drag():
    # Short $10k of a beta-0.8 candidate while the bench earns 7%/yr:
    # drag = -((-10000) * 0.8 * 0.07) / 20000 = +0.028 of book gross per year.
    assert carry_drag_annual(-10_000, 0.8, 0.07, 20_000) == pytest.approx(0.028)


def test_carry_drag_long_positive_beta_is_negative_drag():
    # A long positive-beta overlay is an expected tailwind, not a cost.
    assert carry_drag_annual(10_000, 0.8, 0.07, 20_000) == pytest.approx(-0.028)


def test_carry_drag_long_inverse_is_positive_drag():
    # Long $10k of a beta -1.0 inverse fund: -((10000)*(-1.0)*0.07)/20000 = +0.035.
    assert carry_drag_annual(10_000, -1.0, 0.07, 20_000) == pytest.approx(0.035)


def test_carry_drag_zero_gross_raises():
    with pytest.raises(ValueError):
        carry_drag_annual(-10_000, 0.8, 0.07, 0.0)


def test_borrow_proxy_applies_to_short_and_long_inverse_only():
    # Short: 0.003 * 10000 / 20000 = 0.0015 of book gross per year.
    assert borrow_proxy_annual(-10_000, 0.8, 20_000) == pytest.approx(0.0015)
    # Long positive-beta: no borrow.
    assert borrow_proxy_annual(10_000, 0.8, 20_000) == 0.0
    # Long an inverse (negative-beta) fund: financing/fee proxy applies.
    assert borrow_proxy_annual(10_000, -1.0, 20_000) == pytest.approx(0.0015)
    # The labeled config-style constant is what's applied.
    assert borrow_proxy_annual(-20_000, 1.0, 20_000) == pytest.approx(BORROW_PROXY_RATE)


def test_borrow_proxy_zero_gross_raises():
    with pytest.raises(ValueError):
        borrow_proxy_annual(-10_000, 0.8, 0.0)


def test_protection_per_cost_hand_computed():
    assert protection_per_cost(0.01, 0.005) == pytest.approx(2.0)


def test_protection_per_cost_none_when_cost_nonpositive_or_missing():
    assert protection_per_cost(0.01, 0.0) is None
    assert protection_per_cost(0.01, -0.002) is None
    assert protection_per_cost(None, 0.005) is None
    assert protection_per_cost(0.01, None) is None


# --- bootstrap.py ---


def _daily(seed=3, n=252, scale=0.01):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.0, scale, n))


def test_delta_es_ci_is_deterministic_for_a_seed():
    before, after = _daily(seed=3), _daily(seed=4)
    a = delta_es_ci(before, after, seed=42)
    b = delta_es_ci(before, after, seed=42)
    assert a == b


def test_delta_es_ci_differs_across_seeds():
    before, after = _daily(seed=3), _daily(seed=4)
    assert delta_es_ci(before, after, seed=1) != delta_es_ci(before, after, seed=2)


def test_delta_es_ci_paired_constant_shift_collapses_to_the_shift():
    # ES(x - c) = ES(x) + c for every resample (same ordering), so a PAIRED
    # bootstrap sees delta = ES_b(before) - ES_b(after) = -c in EVERY
    # replicate: the CI collapses to exactly (-c, -c). An unpaired bootstrap
    # cannot produce this — the test pins the pairing property.
    before = _daily(seed=5)
    c = 0.001
    after = before - c
    lo, hi = delta_es_ci(before, after, seed=7)
    assert lo == pytest.approx(-c)
    assert hi == pytest.approx(-c)


def test_delta_es_ci_interval_is_ordered_and_brackets_zero_for_identical_series():
    before = _daily(seed=6)
    lo, hi = delta_es_ci(before, before.copy(), seed=9)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.0)
    lo2, hi2 = delta_es_ci(before, _daily(seed=8), seed=9)
    assert lo2 <= hi2


def test_delta_es_ci_none_when_tail_would_be_empty():
    # 20 obs at 97.5% -> floor(20 * 0.025) = 0 tail obs per replicate.
    assert delta_es_ci(_daily(n=20), _daily(seed=4, n=20)) is None


def test_delta_es_ci_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        delta_es_ci(_daily(n=252), _daily(n=251))


# --- tail.py ---


def test_worst_decile_tail_hand_computed_single_worst_day():
    idx = pd.RangeIndex(10)
    bench = pd.Series([0.01, -0.03, 0.002, 0.004, -0.001, 0.0, 0.02, -0.002, 0.005, 0.003], index=idx)
    book = pd.Series([0.02, -0.05, 0.001, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0], index=idx)
    hedged = pd.Series([0.015, -0.01, 0.001, 0.0, 0.0, 0.0, 0.005, 0.0, 0.0, 0.0], index=idx)
    # floor(10 * 0.1) = 1 worst bench day: index 1 (-0.03).
    stats = worst_decile_tail(book, hedged, bench)
    assert stats.n_days == 1
    assert stats.mean_book == pytest.approx(-0.05)
    assert stats.mean_hedged == pytest.approx(-0.01)


def test_worst_decile_tail_two_worst_days_mean():
    idx = pd.RangeIndex(20)
    bench = pd.Series([0.001] * 20, index=idx)
    bench.iloc[3] = -0.04  # worst
    bench.iloc[11] = -0.02  # second worst
    book = pd.Series([0.0] * 20, index=idx)
    book.iloc[3] = -0.06
    book.iloc[11] = -0.02
    hedged = pd.Series([0.0] * 20, index=idx)
    hedged.iloc[3] = -0.03
    hedged.iloc[11] = -0.01
    stats = worst_decile_tail(book, hedged, bench)
    assert stats.n_days == 2
    assert stats.mean_book == pytest.approx((-0.06 + -0.02) / 2)
    assert stats.mean_hedged == pytest.approx((-0.03 + -0.01) / 2)


def test_worst_decile_tail_inner_joins_on_index():
    bench = pd.Series([-0.03] + [0.001] * 9, index=pd.RangeIndex(10))
    book = pd.Series([-0.05] + [0.0] * 9, index=pd.RangeIndex(10))
    hedged = pd.Series([0.0] * 9, index=pd.RangeIndex(start=1, stop=10))  # day 0 missing
    # After the inner join only days 1..9 remain (9 days) -> floor(0.9) = 0 -> None.
    assert worst_decile_tail(book, hedged, bench) is None


def test_worst_decile_tail_none_when_too_few_days():
    idx = pd.RangeIndex(5)
    s = pd.Series([0.0] * 5, index=idx)
    assert worst_decile_tail(s, s, s) is None


# --- option_hedges.py ---

_SPOT = 452.0
_AS_OF = date(2026, 7, 24)
_EXPIRY = "20261218"  # 147 days out
_T = (date(2026, 12, 18) - _AS_OF).days / 365.25


def _chain():
    rows = []
    for strike, bid, ask in [
        (380.0, 2.0, 2.2),
        (400.0, 4.0, 4.4),
        (430.0, 8.0, 8.4),
        (440.0, 10.0, 10.6),
        (460.0, 16.0, 16.8),
    ]:
        rows.append(
            {"expiry": _EXPIRY, "strike": strike, "right": "P", "bid": bid, "ask": ask,
             "iv": 0.20, "delta": -0.3, "multiplier": 100.0}
        )
    for strike, bid, ask in [(460.0, 9.0, 9.4), (470.0, 6.0, 6.4)]:
        rows.append(
            {"expiry": _EXPIRY, "strike": strike, "right": "C", "bid": bid, "ask": ask,
             "iv": 0.19, "delta": 0.4, "multiplier": 100.0}
        )
    return pd.DataFrame(rows)


def test_build_structures_selects_strikes_and_prices_at_bid_ask():
    structures, notes = build_structures(_chain(), spot=_SPOT, as_of=_AS_OF)
    assert notes == []
    by_kind = {s.kind: s for s in structures}
    assert set(by_kind) == {"protective_put", "put_spread", "collar"}

    # Protective put: long the put closest to 0.95*452 = 429.4 -> K=430, at ASK.
    pp = by_kind["protective_put"]
    assert [(leg.action, leg.strike, leg.right) for leg in pp.legs] == [("long", 430.0, "P")]
    assert pp.expiry == _EXPIRY
    assert pp.expiry_years == pytest.approx(_T)
    assert pp.net_premium_per_contract == pytest.approx(8.4 * 100)

    # Put spread: long 430P at ask, short the put closest to 0.85*452 = 384.2
    # (strictly below the long strike) -> K=380, at BID.
    ps = by_kind["put_spread"]
    assert [(leg.action, leg.strike, leg.right) for leg in ps.legs] == [
        ("long", 430.0, "P"),
        ("short", 380.0, "P"),
    ]
    assert ps.net_premium_per_contract == pytest.approx((8.4 - 2.0) * 100)

    # Collar: long 430P at ask, short the call closest to 1.05*452 = 474.6
    # (strictly above spot) -> K=470, at BID.
    col = by_kind["collar"]
    assert [(leg.action, leg.strike, leg.right) for leg in col.legs] == [
        ("long", 430.0, "P"),
        ("short", 470.0, "C"),
    ]
    assert col.net_premium_per_contract == pytest.approx((8.4 - 6.0) * 100)


def test_build_structures_prefers_expiry_at_least_min_days_out():
    near = _chain().assign(expiry="20260731")  # 7 days out
    far = _chain()
    both = pd.concat([near, far], ignore_index=True)
    structures, _ = build_structures(both, spot=_SPOT, as_of=_AS_OF, min_days=20)
    assert all(s.expiry == _EXPIRY for s in structures)


def test_build_structures_skips_unquotable_structure_with_note():
    chain = _chain()
    chain.loc[(chain["strike"] == 430.0) & (chain["right"] == "P"), "ask"] = np.nan
    structures, notes = build_structures(chain, spot=_SPOT, as_of=_AS_OF)
    # 430P is the long leg of every structure -> next-closest usable strike
    # (440) is used instead of silently dropping everything.
    by_kind = {s.kind: s for s in structures}
    assert by_kind["protective_put"].legs[0].strike == 440.0
    assert notes == []


def test_build_structures_ignores_nan_strike_and_nan_multiplier_rows():
    """Fix round 1 (bundled minor): a corrupt chain row with a NaN strike (or
    NaN multiplier) must never win leg selection. Pre-fix, a NaN-strike row
    iterated FIRST poisoned the closest-strike comparison (every later
    `dist < nan` is False) and its NaN leaked into the structure."""
    import math as _math

    poison = pd.DataFrame(
        [
            {"expiry": _EXPIRY, "strike": np.nan, "right": "P", "bid": 5.0, "ask": 5.4,
             "iv": 0.20, "delta": -0.3, "multiplier": 100.0},
            {"expiry": _EXPIRY, "strike": 435.0, "right": "P", "bid": 7.0, "ask": 7.4,
             "iv": 0.20, "delta": -0.3, "multiplier": np.nan},
        ]
    )
    chain = pd.concat([poison, _chain()], ignore_index=True)  # poison rows FIRST
    structures, _notes = build_structures(chain, spot=_SPOT, as_of=_AS_OF)
    assert structures
    for s in structures:
        for leg in s.legs:
            assert _math.isfinite(leg.strike)
            assert _math.isfinite(leg.multiplier)
    # The healthy 430P (closest usable to 0.95*spot; the NaN-multiplier 435P
    # is closer but corrupt) is still the long leg everywhere.
    pp = next(s for s in structures if s.kind == "protective_put")
    assert pp.legs[0].strike == 430.0


def test_build_structures_empty_chain_returns_note():
    structures, notes = build_structures(_chain().iloc[0:0], spot=_SPOT, as_of=_AS_OF)
    assert structures == []
    assert len(notes) >= 1


def test_build_structures_no_puts_returns_note():
    calls_only = _chain()[_chain()["right"] == "C"].reset_index(drop=True)
    structures, notes = build_structures(calls_only, spot=_SPOT, as_of=_AS_OF)
    assert structures == []
    assert any("put" in n.lower() for n in notes)


def test_size_contracts_off_stress_grid_hand_composed():
    structures, _ = build_structures(_chain(), spot=_SPOT, as_of=_AS_OF)
    pp = next(s for s in structures if s.kind == "protective_put")
    mv = 452_000.0  # 1000 shares at 452
    contracts = size_contracts(pp, mv_underlier=mv, spot=_SPOT, shock=-0.20)

    # Book loss at the -20% stress node = mv * -0.20 (exactly what
    # stress_grid gives a pure-shares book); per-contract structure payoff at
    # the SAME node from stress_grid on the structure's legs.
    book_loss = float(
        stress_grid([], spot=_SPOT, r=0.0, shares=mv / _SPOT, spot_shocks=(-0.20,), vol_shocks=(0.0,)).iloc[0, 0]
    )
    assert book_loss == pytest.approx(mv * -0.20)
    leg = OptionLeg(qty=1.0, strike=430.0, expiry_years=_T, is_call=False, iv=0.20, multiplier=100.0)
    payoff = float(
        stress_grid([leg], spot=_SPOT, r=0.0, spot_shocks=(-0.20,), vol_shocks=(0.0,)).iloc[0, 0]
    )
    assert payoff > 0
    assert contracts == pytest.approx(-book_loss / payoff)


def test_size_contracts_none_when_structure_has_no_downside_payoff():
    # A pure short-call "structure" loses at a downside shock -> unusable size.
    structures, _ = build_structures(_chain(), spot=_SPOT, as_of=_AS_OF)
    pp = next(s for s in structures if s.kind == "protective_put")
    assert size_contracts(pp, mv_underlier=452_000.0, spot=_SPOT, shock=0.05) is None


def test_structure_daily_pnl_composes_bs_price_per_day():
    structures, _ = build_structures(_chain(), spot=_SPOT, as_of=_AS_OF)
    pp = next(s for s in structures if s.kind == "protective_put")
    rets = pd.Series([0.01, -0.02], index=pd.RangeIndex(2))
    pnl = structure_daily_pnl(pp, contracts=2.0, spot=_SPOT, underlier_returns=rets)

    base = bs_price(_SPOT, 430.0, _T, 0.0, 0.20, False)
    for i, r in enumerate([0.01, -0.02]):
        expected = 2.0 * 100.0 * (bs_price(_SPOT * (1 + r), 430.0, _T, 0.0, 0.20, False) - base)
        assert pnl.iloc[i] == pytest.approx(expected)
    # A protective put gains on the down day.
    assert pnl.iloc[1] > 0


def test_premium_annual_drag_hand_computed():
    # $840/contract * 10 contracts / $452k book, annualized over half a year:
    # 8400 / 452000 / 0.5 = 0.0371681...
    assert premium_annual_drag(840.0, 10.0, 452_000.0, 0.5) == pytest.approx(8400.0 / 452_000.0 / 0.5)


def test_premium_annual_drag_guards_degenerate_inputs():
    with pytest.raises(ValueError):
        premium_annual_drag(840.0, 10.0, 0.0, 0.5)
    with pytest.raises(ValueError):
        premium_annual_drag(840.0, 10.0, 452_000.0, 0.0)


def test_option_overlay_reduces_historical_es_on_a_downtrending_book():
    """End-to-end sanity on the overlay convention: a protective put's daily
    P&L (per-original-book-dollar, divided by the ORIGINAL gross) must cut
    historical ES on a series with fat down days."""
    rng = np.random.default_rng(12)
    rets = pd.Series(rng.normal(0.0, 0.01, 252))
    rets.iloc[::25] = -0.04  # periodic crash days
    structures, _ = build_structures(_chain(), spot=_SPOT, as_of=_AS_OF)
    pp = next(s for s in structures if s.kind == "protective_put")
    contracts = size_contracts(pp, mv_underlier=452_000.0, spot=_SPOT)
    pnl = structure_daily_pnl(pp, contracts=contracts, spot=_SPOT, underlier_returns=rets)
    hedged = rets + pnl / 452_000.0
    assert historical_es(hedged) < historical_es(rets)
