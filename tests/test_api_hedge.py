"""API contract tests for the Hedge Lab route: POST /api/hedge ranks sized
hedge candidates against a beta_target objective. Cointegration is a
DIAGNOSTIC column only (Engineering Constraint 12) — ranking is strictly by
protection (ES reduction), never by cointegration. Serialization policy:
UTC ISO timestamps, NaN -> null, unknown symbol/empty candidates/bounds ->
structured 422, never a 500."""

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.options_store import OptionsSnapshotMeta, OptionsStore
from quantmind.datastore.store import BarMeta, BarStore
from quantmind.hedge.cost import BORROW_PROXY_RATE
from quantmind.risk.returns import historical_es, rolling_beta


def _bars(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _flat_bars(n=300, price=100.0):
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.full(n, price)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _beta_correlated_bars(spy_close: pd.Series, beta: float, noise_scale: float, seed: int, start_price=100.0):
    """Build a synthetic instrument whose returns are `beta * spy_returns + idiosyncratic
    noise`, so its true beta vs SPY is deterministically well above the 0.1 unusable
    threshold (unlike two independently-seeded random walks, whose sample beta over a
    finite window can land anywhere near zero by chance)."""
    rng = np.random.default_rng(seed)
    spy_returns = spy_close.pct_change().dropna().to_numpy()
    noise = rng.normal(0, noise_scale, len(spy_returns))
    rets = beta * spy_returns + noise
    close = np.empty(len(spy_close))
    close[0] = start_price
    close[1:] = start_price * np.cumprod(1 + rets)
    idx = spy_close.index
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)  # SPY
    store.write_bars(
        con_id=2, bar_size="1d", bars=_beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2), meta=meta
    )  # QQQ
    store.write_bars(
        con_id=3, bar_size="1d", bars=_beta_correlated_bars(spy_bars["close"], beta=0.5, noise_scale=0.003, seed=3), meta=meta
    )  # IWM
    store.write_bars(con_id=4, bar_size="1d", bars=_flat_bars(), meta=meta)  # FLAT (~zero beta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2, "IWM": 3, "FLAT": 4})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_hedge_single_candidate_sizing_matches_formula(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["benchmark"] == "SPY"
    assert body["n_candidates_evaluated"] == 1
    assert len(body["candidates"]) == 1
    cand = body["candidates"][0]
    assert cand["symbol"] == "QQQ"
    assert cand["unusable"] is False

    # Independently recompute candidate beta the same way the router does:
    # years=1 -> tail 252 rows of cached close, simple returns, inner-joined
    # with the benchmark's returns, rolling_beta(window=60), last value.
    spy_bars = _bars(seed=1)
    qqq_bars = _beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2)
    spy_close = spy_bars["close"].iloc[-252:]
    qqq_close = qqq_bars["close"].iloc[-252:]
    spy_ret = spy_close.pct_change().dropna()
    qqq_ret = qqq_close.pct_change().dropna()
    aligned = pd.concat({"asset": qqq_ret, "bench": spy_ret}, axis=1).dropna()
    beta_series = rolling_beta(aligned["asset"], aligned["bench"], window=60, rf=0.0).dropna()
    beta_cand_expected = float(beta_series.iloc[-1])
    assert cand["beta"] == pytest.approx(beta_cand_expected, rel=1e-9)

    # book == benchmark == SPY exactly -> book_beta is beta of SPY vs itself (~1.0).
    assert body["book_beta"] == pytest.approx(1.0, abs=1e-6)
    book_value = body["book_value"]
    assert book_value == pytest.approx(10 * float(spy_close.iloc[-1]))

    price_cand_last = float(qqq_close.iloc[-1])
    expected_raw_size = (body["book_beta"] - 0.0) * book_value / (beta_cand_expected * price_cand_last)
    expected_hedge_qty = -expected_raw_size
    assert cand["hedge_qty"] == pytest.approx(expected_hedge_qty, rel=1e-6)
    assert cand["hedge_notional"] == pytest.approx(expected_hedge_qty * price_cand_last, rel=1e-6)
    assert cand["es_before"] == body["es_before"]
    assert cand["protection"] is not None


def test_hedge_ranks_candidates_by_protection_per_cost_desc(client):
    # Wave-3B "Hedge honest": the ranking key is protection-per-cost (delta-ES
    # per unit of annual drag). Candidates whose cost is non-positive (a
    # credit/tailwind — protection_per_cost is None by design) fall back to
    # raw-protection ordering after the costed ones; unusable candidates last.
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": -1.0},
            "candidates": ["QQQ", "IWM", "FLAT"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["n_candidates_evaluated"] == 3
    ppcs = [c["protection_per_cost"] for c in body["candidates"]]
    protections = [c["protection"] for c in body["candidates"]]
    # Phase 1: non-None protection_per_cost first, sorted descending.
    seen_ppc_none = False
    prev_ppc = None
    for ppc in ppcs:
        if ppc is None:
            seen_ppc_none = True
            continue
        assert not seen_ppc_none, "a non-None protection_per_cost appeared after a None one"
        if prev_ppc is not None:
            assert ppc <= prev_ppc
        prev_ppc = ppc
    # Phase 2: among the ppc-None tail, protection (if any) sorted descending.
    tail_prot = [p for ppc, p in zip(ppcs, protections) if ppc is None]
    non_none_tail = [p for p in tail_prot if p is not None]
    assert non_none_tail == sorted(non_none_tail, reverse=True)
    # FLAT is unusable (~zero beta) so it must be flagged and ranked last.
    flat = next(c for c in body["candidates"] if c["symbol"] == "FLAT")
    assert flat["unusable"] is True
    assert body["candidates"][-1]["symbol"] == "FLAT"


def test_hedge_unusable_candidate_flagged_not_errored(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FLAT"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    cand = body["candidates"][0]
    assert cand["symbol"] == "FLAT"
    assert cand["unusable"] is True
    assert abs(cand["beta"]) < 0.1
    assert cand["hedge_qty"] is None
    assert cand["hedge_notional"] is None
    assert cand["protection"] is None


def test_hedge_default_candidate_universe_excludes_book_symbols(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    symbols = {c["symbol"] for c in body["candidates"]}
    assert "SPY" not in symbols
    assert symbols == {"QQQ", "IWM", "FLAT"}


def test_hedge_unknown_book_symbol_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "NOPE", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_hedge_unknown_candidate_symbol_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["NOPE"],
        },
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_hedge_empty_candidate_universe_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [
                {"symbol": "SPY", "qty": 10},
                {"symbol": "QQQ", "qty": 1},
                {"symbol": "IWM", "qty": 1},
                {"symbol": "FLAT", "qty": 1},
            ],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422
    assert "detail" in r.json()


def test_hedge_book_bounds_reject_empty_and_oversized(client):
    r = client.post(
        "/api/hedge",
        json={"book": [], "objective": {"kind": "beta_target", "value": 0.0}},
    )
    assert r.status_code == 422

    r2 = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": i + 1} for i in range(51)],
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r2.status_code == 422


def test_hedge_objective_value_bounds_reject_out_of_range(client):
    r = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 3.0}},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": -3.0}},
    )
    assert r2.status_code == 422


def test_hedge_protection_not_inflated_by_large_hedge_notional(tmp_path):
    """I1 regression: es_after must be computed by overlaying the hedge on
    the ORIGINAL book (normalized by the original book's gross), never by
    re-normalizing a blended book+hedge portfolio by the NEW (hedge-inflated)
    gross. The old approach mechanically shrinks the hedge leg's weight
    whenever its notional is large relative to the book — which happens for
    any low-beta candidate, since sizing (hedge_qty ∝ 1/beta_cand) blows up
    as beta_cand shrinks. That let a low-beta, weakly-correlated candidate
    ("WEAK") look MORE protective than a well-correlated, sanely-sized one
    ("GOOD") purely because its huge notional dominated the blended gross and
    drowned out the book's own risk in the denominator — not because it
    actually reduces the book's tail risk. Pre-fix, this assertion fails
    (WEAK's protection > GOOD's); post-fix WEAK must not beat GOOD."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)  # SPY (book)
    store.write_bars(
        con_id=2,
        bar_size="1d",
        bars=_beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2),
        meta=meta,
    )  # GOOD: well-correlated, sanely-sized hedge
    store.write_bars(
        con_id=3,
        bar_size="1d",
        # Low beta (just above the 0.1 unusable floor) + tiny idiosyncratic
        # noise -> low own-volatility AND a huge required notional (sizing
        # is inversely proportional to beta_cand). This combination is what
        # exploited the old normalization: dominating the blended gross with
        # a low-vol instrument mechanically crushed es_after towards zero.
        bars=_beta_correlated_bars(spy_bars["close"], beta=0.15, noise_scale=0.0005, seed=3),
        meta=meta,
    )  # WEAK
    store.write_symbol_map({"SPY": 1, "GOOD": 2, "WEAK": 3})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["GOOD", "WEAK"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    by_symbol = {c["symbol"]: c for c in body["candidates"]}
    good, weak = by_symbol["GOOD"], by_symbol["WEAK"]
    assert good["unusable"] is False
    assert weak["unusable"] is False

    # WEAK needs a much larger notional than GOOD to hit the same beta
    # target (sizing ∝ 1/beta_cand, and 0.15 << 0.8) — that's the setup for
    # the denominator-inflation exploit, not the bug itself.
    assert abs(weak["hedge_notional"]) > abs(good["hedge_notional"])

    # The bug: WEAK's protection must not exceed GOOD's purely because its
    # huge notional inflated (old code) or dominates (this is the honest
    # overlay check) the denominator. A weakly-correlated, low-beta hedge is
    # not a better hedge just because it's sized bigger.
    assert weak["protection"] is not None and good["protection"] is not None
    assert weak["protection"] <= good["protection"]


def test_hedge_years_bounds_reject_out_of_range(client):
    r = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 0.0}, "years": 0},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/hedge",
        json={"book": [{"symbol": "SPY", "qty": 10}], "objective": {"kind": "beta_target", "value": 0.0}, "years": 100},
    )
    assert r2.status_code == 422


def test_hedge_response_has_no_cointegration_column(client):
    # Pre-wave-3 consolidation pass (TODOS.md): cointegration's home is Lab's
    # pair pipeline now, never the Hedge Lab response.
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    assert "coint_pvalue" not in r.json()["candidates"][0]


def test_hedge_nonfinite_book_last_close_is_422_naming_the_symbol(tmp_path):
    # Aligned with routers/whatif.py's identical guard (pre-wave-3
    # consolidation pass, TODOS.md): a NaN last close on a BOOK leg must
    # never silently propagate into book_beta/es_before/protection.
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    bad_bars = _bars(seed=2)
    bad_bars.loc[bad_bars.index[-1], "close"] = np.nan
    store.write_bars(con_id=2, bar_size="1d", bars=bad_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = c.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "QQQ", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["SPY"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]


def test_hedge_zero_gross_book_is_422(client):
    # Two offsetting legs in the same symbol net to zero market value.
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}, {"symbol": "SPY", "qty": -10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "zero gross" in r.json()["detail"]


# --- book_ref (wave-3 Task A1's book-flow spine): an alternative to inline
# `book`, resolved via routers/book.py's pinned snapshots. ---


def test_hedge_book_ref_resolves_to_the_same_result_as_inline_book(client):
    pinned = client.post("/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 10}]}).json()

    r_ref = client.post(
        "/api/hedge",
        json={
            "book_ref": pinned["snapshot_id"],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    r_inline = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r_ref.status_code == r_inline.status_code == 200
    assert r_ref.json() == r_inline.json()


def test_hedge_unknown_book_ref_is_422(client):
    r = client.post(
        "/api/hedge",
        json={"book_ref": "does-not-exist", "objective": {"kind": "beta_target", "value": 0.0}},
    )
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_hedge_both_book_and_book_ref_is_422(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "book_ref": "whatever",
            "objective": {"kind": "beta_target", "value": 0.0},
        },
    )
    assert r.status_code == 422


def test_hedge_neither_book_nor_book_ref_is_422(client):
    r = client.post("/api/hedge", json={"objective": {"kind": "beta_target", "value": 0.0}})
    assert r.status_code == 422


# --- wave-3B "Hedge honest": cost columns, delta-ES CI, tail-conditional
# protection, option hedge candidates on the dominant underlier. ---


def _run_qqq(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    return r.json()


def test_hedge_cost_columns_match_hand_recomputed_formula(client):
    body = _run_qqq(client)
    cand = body["candidates"][0]

    # E[r_bench] annualized from the same cached bars/window the router used.
    spy_close = _bars(seed=1)["close"].iloc[-252:]
    er_expected = float(spy_close.pct_change().dropna().mean()) * 252
    assert body["bench_expected_return_annual"] == pytest.approx(er_expected, rel=1e-9)

    gross = abs(body["book_value"])  # single long position: gross == book_value
    carry_expected = -(cand["hedge_notional"] * cand["beta"] * er_expected) / gross
    assert cand["carry_drag_annual"] == pytest.approx(carry_expected, rel=1e-9)

    # QQQ hedge to beta 0 from a long book is a SHORT -> borrow proxy applies.
    assert cand["hedge_notional"] < 0
    borrow_expected = BORROW_PROXY_RATE * abs(cand["hedge_notional"]) / gross
    assert cand["borrow_proxy_annual"] == pytest.approx(borrow_expected, rel=1e-9)

    assert cand["cost_annual"] == pytest.approx(carry_expected + borrow_expected, rel=1e-9)
    if cand["cost_annual"] > 0:
        assert cand["protection_per_cost"] == pytest.approx(
            cand["protection"] / cand["cost_annual"], rel=1e-9
        )
    else:
        assert cand["protection_per_cost"] is None

    # The cost methodology is labeled: a proxy, never a quoted borrow rate.
    assert "proxy" in body["cost_note"].lower()


def test_hedge_delta_es_ci_present_ordered_and_deterministic(client):
    # Global Constraint (wave-3): any bootstrap statistic shows its interval.
    body1 = _run_qqq(client)
    body2 = _run_qqq(client)
    cand = body1["candidates"][0]
    assert cand["delta_es_ci_low"] is not None
    assert cand["delta_es_ci_high"] is not None
    assert cand["delta_es_ci_low"] <= cand["delta_es_ci_high"]
    # Seeded bootstrap: two identical requests give identical intervals.
    assert body1["candidates"][0]["delta_es_ci_low"] == body2["candidates"][0]["delta_es_ci_low"]
    assert body1["candidates"][0]["delta_es_ci_high"] == body2["candidates"][0]["delta_es_ci_high"]
    assert "bootstrap" in body1["ci_note"].lower()


def test_hedge_tail_conditional_fields_hand_recomputed(client):
    body = _run_qqq(client)
    cand = body["candidates"][0]

    # Book == benchmark == SPY here, so the worst-decile bench days ARE the
    # book's worst days; recompute the un-hedged tail mean by hand.
    spy_ret = _bars(seed=1)["close"].iloc[-252:].pct_change().dropna()
    qqq_ret = (
        _beta_correlated_bars(_bars(seed=1)["close"], beta=0.8, noise_scale=0.002, seed=2)["close"]
        .iloc[-252:]
        .pct_change()
        .dropna()
    )
    aligned = pd.concat({"book": spy_ret, "cand": qqq_ret}, axis=1).dropna()
    n_tail = math.floor(len(aligned) * 0.10 + 1e-9)
    worst = aligned.nsmallest(n_tail, "book")  # bench == book for this fixture
    assert cand["tail_n_days"] == n_tail
    assert cand["tail_mean_book"] == pytest.approx(float(worst["book"].mean()), rel=1e-9)
    # Hedged tail mean must beat the un-hedged one for a short-QQQ hedge.
    assert cand["tail_mean_hedged"] > cand["tail_mean_book"]
    assert "worst-decile" in body["tail_note"].lower()
    assert "daily" in body["tail_note"].lower()


def test_hedge_unusable_candidate_has_null_cost_and_tail_fields(client):
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["FLAT"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    cand = r.json()["candidates"][0]
    assert cand["unusable"] is True
    for field in (
        "carry_drag_annual",
        "borrow_proxy_annual",
        "cost_annual",
        "protection_per_cost",
        "delta_es_ci_low",
        "delta_es_ci_high",
        "tail_mean_book",
        "tail_mean_hedged",
    ):
        assert cand[field] is None, field


def test_hedge_option_note_when_no_cached_chain(client):
    # SPY's chain is empty upstream (wave-3 plan): degrade honestly with a
    # structured note, never a 500 — and never silently omit the section.
    body = _run_qqq(client)
    assert body["option_underlier"] == "SPY"
    assert body["option_hedges"] == []
    assert body["option_note"] is not None
    assert "chain" in body["option_note"].lower()


def _option_client(tmp_path, qty=1000, poison_nan_strike=False):
    """Client whose store carries BOTH bars and a cached SPY chain with
    strikes placed relative to the fixture's actual last close.
    `poison_nan_strike` prepends a corrupt NaN-strike put row FIRST (the
    order that used to let it win closest-strike selection — fix round 1)."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    store.write_bars(
        con_id=2,
        bar_size="1d",
        bars=_beta_correlated_bars(spy_bars["close"], beta=0.8, noise_scale=0.002, seed=2),
        meta=meta,
    )
    store.write_symbol_map({"SPY": 1, "QQQ": 2})

    spot = float(spy_bars["close"].iloc[-1])
    rows = []
    if poison_nan_strike:
        rows.append(
            {"expiry": "20261218", "strike": np.nan, "right": "P",
             "bid": 5.0, "ask": 5.4, "iv": 0.20, "delta": -0.3, "multiplier": 100.0}
        )
    for frac, bid, ask in [(0.80, 2.0, 2.2), (0.85, 3.0, 3.3), (0.95, 6.0, 6.4)]:
        rows.append(
            {"expiry": "20261218", "strike": round(frac * spot, 2), "right": "P",
             "bid": bid, "ask": ask, "iv": 0.20, "delta": -0.3, "multiplier": 100.0}
        )
    rows.append(
        {"expiry": "20261218", "strike": round(1.05 * spot, 2), "right": "C",
         "bid": 4.0, "ask": 4.4, "iv": 0.19, "delta": 0.4, "multiplier": 100.0}
    )
    OptionsStore(store.root).write_chain(
        "SPY", pd.DataFrame(rows), OptionsSnapshotMeta(as_of="2026-07-24", spot=spot)
    )
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})
    return client, spot


def test_hedge_option_candidates_built_from_cached_chain(tmp_path):
    client, spot = _option_client(tmp_path)
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["option_underlier"] == "SPY"
    assert body["option_chain_as_of"] == "2026-07-24"
    kinds = [o["kind"] for o in body["option_hedges"]]
    assert set(kinds) == {"protective_put", "put_spread", "collar"}
    for o in body["option_hedges"]:
        assert o["contracts"] is not None and o["contracts"] > 0
        assert o["expiry"] == "20261218"
        assert o["es_before"] == body["es_before"]
        assert o["es_after"] is not None
        assert o["protection"] is not None
        # Premium drag: net premium (long at ask, short at bid) annualized
        # over time-to-expiry, as a fraction of book gross per year.
        assert o["cost_annual"] is not None
        assert o["delta_es_ci_low"] is not None and o["delta_es_ci_high"] is not None
        assert o["delta_es_ci_low"] <= o["delta_es_ci_high"]
        assert o["tail_mean_book"] is not None and o["tail_mean_hedged"] is not None
        # Protective structures must improve the tail-conditional mean.
        assert o["tail_mean_hedged"] > o["tail_mean_book"]
        assert len(o["legs"]) >= 1
    # Ranked by protection-per-cost desc (None-cost structures after costed ones).
    ppcs = [o["protection_per_cost"] for o in body["option_hedges"]]
    non_none = [p for p in ppcs if p is not None]
    assert non_none == sorted(non_none, reverse=True)
    assert all(p is None for p in ppcs[len(non_none):])

    # Protective put premium hand-check: long the 0.95*spot put at ASK.
    pp = next(o for o in body["option_hedges"] if o["kind"] == "protective_put")
    assert pp["net_premium_per_contract"] == pytest.approx(6.4 * 100)
    assert pp["legs"][0]["strike"] == pytest.approx(round(0.95 * spot, 2))


def test_hedge_option_note_when_dominant_underlier_is_short(tmp_path):
    client, _spot = _option_client(tmp_path)
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": -1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["option_hedges"] == []
    assert body["option_note"] is not None
    assert "short" in body["option_note"].lower()


def test_hedge_empty_chain_is_structured_note_never_500(tmp_path):
    client, spot = _option_client(tmp_path)
    # Overwrite with an EMPTY chain (the "cached but empty upstream" case).
    empty = pd.DataFrame(
        columns=["expiry", "strike", "right", "bid", "ask", "iv", "delta", "multiplier"]
    )
    store_root = None
    # Reach the store root through the app the fixture built.
    store_root = client.app.state.store.root  # type: ignore[attr-defined]
    OptionsStore(store_root).write_chain("SPY", empty, OptionsSnapshotMeta(as_of="2026-07-24", spot=spot))
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["option_hedges"] == []
    assert body["option_note"] is not None


def test_hedge_horizon_labels_present(client):
    body = _run_qqq(client)
    # Every risk number renders with its horizon (wave-3 Global Constraint).
    assert "daily" in body["es_note"].lower()
    assert "/yr" in body["cost_note"] or "per year" in body["cost_note"].lower() or "annual" in body["cost_note"].lower()


# --- Batch-2 final review, item 2: option-leg parity. A pinned/inline book
# of option legs must price at qty x effective-multiplier x underlier close
# (whatif's delta-one convention), never silently as bare shares (the 100x
# understatement), and the approximation must be declared in `notes`. ---


_OPT_BOOK = [{"symbol": "SPY", "qty": 5, "strike": 400.0, "expiry": "20260918", "right": "C"}]
_STK_BOOK = [{"symbol": "SPY", "qty": 5}]


def _run_book(client, book=None, book_ref=None):
    body = {
        "objective": {"kind": "beta_target", "value": 0.0},
        "candidates": ["QQQ"],
        "years": 1,
    }
    if book is not None:
        body["book"] = book
    if book_ref is not None:
        body["book_ref"] = book_ref
    r = client.post("/api/hedge", json=body)
    assert r.status_code == 200
    return r.json()


def test_hedge_full_opt_book_prices_at_multiplier_scaled_notional_with_note(client):
    # 5 SPY calls (multiplier 100) carry the underlier notional of 500 shares
    # — pre-fix this returned book_value byte-identical to the 5-share book,
    # silently, with no note.
    body_opt = _run_book(client, book=_OPT_BOOK)
    body_stk = _run_book(client, book=_STK_BOOK)
    assert body_opt["book_value"] == pytest.approx(100.0 * body_stk["book_value"], rel=1e-9)
    assert any("delta-one" in n for n in body_opt["notes"])
    assert body_stk["notes"] == []


def test_hedge_option_book_ref_matches_inline(client):
    pinned = client.post("/api/book/pin", json={"positions": _OPT_BOOK}).json()
    body_ref = _run_book(client, book_ref=pinned["snapshot_id"])
    body_inline = _run_book(client, book=_OPT_BOOK)
    assert body_ref == body_inline


def test_hedge_inline_partial_option_descriptor_is_422(client):
    # Same all-or-none guard as whatif's inline path: with multiplier-aware
    # pricing keyed on `right`, a right-only leg would otherwise silently
    # price at 100x.
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 5, "right": "C"}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 422
    assert "SPY" in r.json()["detail"]
    assert "together" in r.json()["detail"]


# --- fix round 1 ---


def test_hedge_displayed_protection_shares_one_window_with_its_ci(tmp_path):
    """MUST-FIX regression (review round 1): a candidate whose cached history
    is a strict, CALMER subset of the book's window must not show phantom
    protection driven purely by window truncation. The displayed ΔES, its
    bootstrap CI, protection_per_cost and the tail stats must all share the
    book∩candidate window; the full-window ES stays the book-level headline
    only. Pre-fix, protection = es_before(full window, crash days included)
    − es_after(calm subset) — a Δ that lies entirely OUTSIDE its own 95% CI."""
    rng = np.random.default_rng(21)
    idx = pd.bdate_range(end="2026-07-24", periods=300)
    spy_ret = rng.normal(0.0, 0.01, 300)
    # Crash days land INSIDE the 1y book window (last 252 bars = idx 48..299)
    # but BEFORE the candidate's history begins (last 150 bars = idx 150..299).
    spy_ret[60:130:10] = -0.05
    spy_close = 100 * np.cumprod(1 + spy_ret)
    spy_bars = pd.DataFrame(
        {"open": spy_close, "high": spy_close, "low": spy_close, "close": spy_close, "volume": 1000.0},
        index=idx,
    )

    # Candidate: correlated (beta 0.8) with SPY over its own 150-day history.
    spy_close_s = pd.Series(spy_close, index=idx)
    sub_ret = spy_close_s.iloc[150:].pct_change().dropna().to_numpy()
    cand_ret = 0.8 * sub_ret + rng.normal(0, 0.002, len(sub_ret))
    cand_close = np.empty(150)
    cand_close[0] = 100.0
    cand_close[1:] = 100.0 * np.cumprod(1 + cand_ret)
    cand_bars = pd.DataFrame(
        {"open": cand_close, "high": cand_close, "low": cand_close, "close": cand_close, "volume": 1000.0},
        index=idx[150:],
    )

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=cand_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "NEWER": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["NEWER"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    cand = body["candidates"][0]
    assert cand["unusable"] is False

    # Hand-recompute the ALIGNED-window ES of the un-hedged book: book returns
    # (1y window) inner-joined with the candidate's return days.
    book_ret = spy_close_s.iloc[-252:].pct_change().dropna()
    cand_ret_s = pd.Series(cand_close, index=idx[150:]).pct_change().dropna()
    aligned = pd.concat({"book": book_ret, "cand": cand_ret_s}, axis=1).dropna()
    es_before_aligned = historical_es(aligned["book"], confidence=0.975)

    # The headline stays the full crash-laden window; the candidate row's
    # es_before is the window-consistent (calmer) one — they must differ here.
    assert body["es_before"] > es_before_aligned + 0.005
    assert cand["es_before"] == pytest.approx(es_before_aligned, rel=1e-9)

    # (a) the displayed protection lies inside its own 95% CI…
    assert cand["protection"] == pytest.approx(cand["es_before"] - cand["es_after"], rel=1e-9)
    assert cand["delta_es_ci_low"] <= cand["protection"] <= cand["delta_es_ci_high"]
    # …and the ranking key inherits the window-consistent Δ.
    assert cand["protection_per_cost"] is None or cand["protection_per_cost"] == pytest.approx(
        cand["protection"] / cand["cost_annual"], rel=1e-9
    )

    # (b) the OLD mismatched-window Δ (full-window es_before − aligned
    # es_after) is the phantom the reviewer described: it falls entirely
    # outside the CI of the statistic it pretended to be.
    phantom = body["es_before"] - cand["es_after"]
    assert not (cand["delta_es_ci_low"] <= phantom <= cand["delta_es_ci_high"])


def test_hedge_short_history_candidate_reports_aligned_n_obs_and_note_says_so(tmp_path):
    """Batch-2 final review item 6: es_note claimed ONE window but candidate
    rows have used the book∩candidate aligned window since f3f3408. The note
    must say so, and each candidate must report its own aligned n_obs so the
    truncation is auditable per row."""
    rng = np.random.default_rng(21)
    idx = pd.bdate_range(end="2026-07-24", periods=300)
    spy_ret = rng.normal(0.0, 0.01, 300)
    spy_close = 100 * np.cumprod(1 + spy_ret)
    spy_bars = pd.DataFrame(
        {"open": spy_close, "high": spy_close, "low": spy_close, "close": spy_close, "volume": 1000.0},
        index=idx,
    )
    # Candidate history: only the last 150 bars -> 149 return days.
    spy_close_s = pd.Series(spy_close, index=idx)
    sub_ret = spy_close_s.iloc[150:].pct_change().dropna().to_numpy()
    cand_ret = 0.8 * sub_ret + rng.normal(0, 0.002, len(sub_ret))
    cand_close = np.empty(150)
    cand_close[0] = 100.0
    cand_close[1:] = 100.0 * np.cumprod(1 + cand_ret)
    cand_bars = pd.DataFrame(
        {"open": cand_close, "high": cand_close, "low": cand_close, "close": cand_close, "volume": 1000.0},
        index=idx[150:],
    )

    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=cand_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "NEWER": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 10}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["NEWER"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    cand = body["candidates"][0]
    # 150 candidate bars -> 149 return days, all inside the 1y book window.
    assert cand["n_obs"] == 149
    # The headline note now discloses the per-candidate aligned window.
    assert "aligned" in body["es_note"]
    assert "fewer observations" in body["es_note"]


def test_hedge_option_rows_report_aligned_n_obs(tmp_path):
    client, _spot = _option_client(tmp_path)
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    for o in r.json()["option_hedges"]:
        assert o["n_obs"] is not None and o["n_obs"] > 0


def test_hedge_nan_strike_chain_row_never_reaches_the_response(tmp_path):
    """Bundled minor 1: a corrupt NaN-strike chain row must not win leg
    selection, and no leg float may reach the JSON un-cleaned (NaN is not
    valid JSON)."""
    client, spot = _option_client(tmp_path, poison_nan_strike=True)
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    assert "NaN" not in r.text  # invalid-JSON NaN literal must never be emitted
    body = r.json()
    assert body["option_hedges"], "structures must still build from the healthy rows"
    for o in body["option_hedges"]:
        for leg in o["legs"]:
            assert leg["strike"] is not None and math.isfinite(leg["strike"])
            assert leg["price"] is not None and math.isfinite(leg["price"])
    # The healthy 0.95*spot put must be the long leg, not the poison row.
    pp = next(o for o in body["option_hedges"] if o["kind"] == "protective_put")
    assert pp["legs"][0]["strike"] == pytest.approx(round(0.95 * spot, 2))


def test_hedge_unparseable_chain_as_of_is_honest_note_not_silent_today(tmp_path):
    """Batch-2 final review item 7i: _chain_as_of_date used to swallow a
    ValueError and silently return date.today() — an unparseable snapshot
    date silently repriced every premium/time-to-expiry as if snapped NOW.
    Degrade to a structured note instead."""
    client, spot = _option_client(tmp_path)
    store_root = client.app.state.store.root  # type: ignore[attr-defined]
    # Rewrite the chain meta with garbage as_of.
    chain_df, _meta = OptionsStore(store_root).read_chain("SPY")
    OptionsStore(store_root).write_chain(
        "SPY", chain_df, OptionsSnapshotMeta(as_of="not-a-date", spot=spot)
    )
    r = client.post(
        "/api/hedge",
        json={
            "book": [{"symbol": "SPY", "qty": 1000}],
            "objective": {"kind": "beta_target", "value": 0.0},
            "candidates": ["QQQ"],
            "years": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["option_hedges"] == []
    assert body["option_note"] is not None
    assert "as_of" in body["option_note"] or "unparseable" in body["option_note"].lower()


def test_hedge_option_note_distinguishes_no_overlap_from_no_payoff(tmp_path):
    """Bundled minor 2: when the book's return window and the dominant
    underlier's bars have NO overlapping days, the degrade note must say so —
    not falsely claim the structures had 'no payoff at the stress node'."""
    import inspect

    from quantmind.api.routers.hedge import _build_option_hedges

    store = BarStore(tmp_path)
    spot = 452.0
    rows = [
        {"expiry": "20261218", "strike": round(0.95 * spot, 2), "right": "P",
         "bid": 6.0, "ask": 6.4, "iv": 0.20, "delta": -0.3, "multiplier": 100.0},
    ]
    OptionsStore(store.root).write_chain(
        "SPY", pd.DataFrame(rows), OptionsSnapshotMeta(as_of="2026-07-24", spot=spot)
    )

    dom_idx = pd.bdate_range(end="2026-07-24", periods=50)
    book_idx = pd.bdate_range(end="2020-01-03", periods=50)  # disjoint, years earlier
    kwargs = dict(
        store=store,
        dominant="SPY",
        mv_dominant=452_000.0,
        dominant_prices=pd.Series(np.linspace(440.0, 452.0, 50), index=dom_idx),
        book_returns=pd.Series(0.001, index=book_idx),
        bench_returns=pd.Series(0.001, index=book_idx),
        book_gross=452_000.0,
    )
    # Signature-agnostic: the round-1 fix drops the now-unused es_before
    # parameter; the red run (pre-fix) still needs to pass it.
    if "es_before" in inspect.signature(_build_option_hedges).parameters:
        kwargs["es_before"] = 0.02
    hedges, note, _chain_as_of = _build_option_hedges(**kwargs)
    assert hedges == []
    assert note is not None
    assert "overlap" in note.lower()
    assert "stress node" not in note.lower()
