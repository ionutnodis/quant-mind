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
from quantmind.risk.returns import rolling_beta


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


def _option_client(tmp_path, qty=1000):
    """Client whose store carries BOTH bars and a cached SPY chain with
    strikes placed relative to the fixture's actual last close."""
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
