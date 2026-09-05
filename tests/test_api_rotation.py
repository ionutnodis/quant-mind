"""API contract tests for POST /api/rotation: universe picker, clustered
correlation ordering, per-symbol returns, and the anchor-driven "other side
of the trade" ranking. Store-only, never a 500 (Global Constraints); unknown
symbols -> 422, a mapped-but-unsynced symbol degrades into `missing`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.api.routers.macro import FACTORS, SECTORS
from quantmind.api.routers.rotation import OtherSideOut, cluster_order, rank_other_side
from quantmind.datastore.store import BarMeta, BarStore

N_BARS = 150


def _bars_from_returns(daily_returns: np.ndarray, price0: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-24", periods=len(daily_returns) + 1)
    close = price0 * np.cumprod(np.concatenate([[1.0], 1.0 + daily_returns]))
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _client(store: BarStore) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


@pytest.fixture
def rng():
    return np.random.default_rng(7)


@pytest.fixture
def store(tmp_path, rng) -> BarStore:
    """Two correlated blocks: {A,B} share a common factor (correlated,
    A/B both trend up), {C,D} share a different, negatively-correlated
    factor (C/D trend down) — hand-checkable clustering and a clean
    "other side of the trade" case (anchor a down mover, expect the
    uncorrelated-and-up symbol to rank first)."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    # A single shared factor drives both blocks (A,B long it, C,D short it),
    # so A/B and C/D are each strongly positively correlated WITHIN their
    # block and strongly NEGATIVELY correlated ACROSS blocks (unlike two
    # independent random drifts, which would only be weakly correlated).
    factor = rng.normal(0.0, 0.01, N_BARS)
    idiosyncratic = lambda: rng.normal(0.0, 0.002, N_BARS)  # noqa: E731

    series = {
        "A": 0.004 + factor + idiosyncratic(),
        "B": 0.004 + factor + idiosyncratic(),
        "C": -0.004 - factor + idiosyncratic(),
        "D": -0.004 - factor + idiosyncratic(),
    }
    for i, (symbol, rets) in enumerate(series.items(), start=1):
        store.write_bars(con_id=i, bar_size="1d", bars=_bars_from_returns(rets), meta=meta)
    store.write_symbol_map({s: i for i, s in enumerate(series, start=1)})
    return store


def test_custom_universe_requires_symbols(store):
    r = _client(store).post("/api/rotation", json={"universe": "custom"})
    assert r.status_code == 422


def test_unknown_symbol_is_422(store):
    r = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A", "GHOST"]}
    )
    assert r.status_code == 422
    assert "GHOST" in r.json()["detail"]


def test_mapped_symbol_without_bars_is_skipped_not_500(store):
    store.write_symbol_map({**store.read_symbol_map(), "UNSYNCED": 999})
    r = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A", "B", "UNSYNCED"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert "UNSYNCED" not in body["symbols"]
    assert "UNSYNCED" in body["missing"]


def test_future_dated_rotation_symbol_is_missing_not_ranked(store):
    future = pd.bdate_range(
        start=datetime.now(UTC).date() + timedelta(days=1), periods=1
    )[0].date()
    bars, meta = store.read_bars(con_id=1, bar_size="1d")
    bars.index = pd.bdate_range(end=future, periods=len(bars))
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=bars,
        meta=BarMeta(
            bar_type=meta.bar_type, adjusted_asof=future.isoformat()
        ),
    )

    response = _client(store).post(
        "/api/rotation",
        json={"universe": "custom", "symbols": ["A", "B"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["symbols"] == ["B"]
    assert body["missing"] == ["A"]
    assert body["as_of"] == "2026-07-24T00:00:00Z"


def test_custom_universe_returns_clustered_matrix_and_returns(store):
    r = _client(store).post(
        "/api/rotation",
        json={"universe": "custom", "symbols": ["A", "B", "C", "D"], "corr_window": 60, "return_days": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["symbols"]) == {"A", "B", "C", "D"}
    assert body["corr_window"] == 60
    assert body["return_days"] == 5
    assert body["as_of"].endswith("Z")
    assert body["missing"] == []

    # clustered order groups the correlated block adjacently: {A,B} together
    # and {C,D} together (not necessarily A,B,C,D verbatim, but never
    # interleaved like A,C,B,D).
    order = body["symbols"]
    idx = {s: i for i, s in enumerate(order)}
    assert abs(idx["A"] - idx["B"]) == 1
    assert abs(idx["C"] - idx["D"]) == 1

    n = len(order)
    assert len(body["matrix"]) == n
    assert all(len(row) == n for row in body["matrix"])
    for i in range(n):
        assert body["matrix"][i][i] == pytest.approx(1.0, abs=1e-6)

    returns_by_symbol = {row["symbol"]: row["ret"] for row in body["returns"]}
    assert set(returns_by_symbol) == {"A", "B", "C", "D"}
    assert returns_by_symbol["A"] > 0
    assert returns_by_symbol["C"] < 0


def test_default_sector_and_factor_universes_are_recognized(store):
    # sectors/factors default membership won't be cached in this fixture's
    # store, so every symbol degrades into `missing` — the point of this
    # test is that the *default universe resolution* itself works (200, not
    # a crash), matching macro.py's "empty store -> 200 all missing" posture.
    r = _client(store).post("/api/rotation", json={"universe": "sectors"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["missing"]) == set(SECTORS)
    assert body["symbols"] == []

    r2 = _client(store).post("/api/rotation", json={"universe": "factors"})
    assert r2.status_code == 200
    assert set(r2.json()["missing"]) == set(FACTORS)


def test_anchor_other_side_ranks_uncorrelated_and_up_first(store):
    r = _client(store).post(
        "/api/rotation",
        json={"universe": "custom", "symbols": ["A", "B", "C", "D"], "corr_window": 60, "anchor": "C"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["anchor"] == "C"
    other_side = body["other_side"]
    assert other_side is not None
    ranked_symbols = [row["symbol"] for row in other_side]
    assert set(ranked_symbols) == {"A", "B", "D"}
    # C is down. D is positively correlated with C AND also down (same
    # down-move, not a rotation destination) -> score clips to 0. A/B are
    # negatively correlated with C AND up -> positive score, ranked first.
    assert ranked_symbols[0] in {"A", "B"}
    assert ranked_symbols[-1] == "D"
    top = other_side[0]
    assert top["corr"] < 0
    assert top["ret"] > 0
    assert top["score"] > 0
    d_row = next(row for row in other_side if row["symbol"] == "D")
    assert d_row["score"] == pytest.approx(0.0)


def test_unknown_anchor_is_422(store):
    r = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A", "B"], "anchor": "GHOST"}
    )
    assert r.status_code == 422


def test_bounds_reject_bad_corr_window_and_return_days(store):
    r = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A"], "corr_window": 45}
    )
    assert r.status_code == 422

    r2 = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A"], "return_days": 0}
    )
    assert r2.status_code == 422

    r3 = _client(store).post(
        "/api/rotation", json={"universe": "custom", "symbols": ["A"], "return_days": 22}
    )
    assert r3.status_code == 422


def test_empty_store_never_500(tmp_path):
    empty_store = BarStore(tmp_path)
    r = _client(empty_store).post("/api/rotation", json={"universe": "world"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbols"] == []
    assert body["matrix"] == []
    assert len(body["missing"]) == 10


# --- cluster_order unit tests (pure helper, hand-computed) ---


def _corr_df(labels, matrix):
    return pd.DataFrame(matrix, index=labels, columns=labels)


def test_cluster_order_two_symbols_returns_as_is():
    corr = _corr_df(["X", "Y"], [[1.0, 0.5], [0.5, 1.0]])
    assert cluster_order(corr) == ["X", "Y"]


def test_cluster_order_groups_correlated_block_adjacently():
    # A/B highly correlated, C/D highly correlated, the two blocks anti-correlated.
    labels = ["A", "B", "C", "D"]
    matrix = [
        [1.0, 0.9, -0.8, -0.7],
        [0.9, 1.0, -0.75, -0.8],
        [-0.8, -0.75, 1.0, 0.85],
        [-0.7, -0.8, 0.85, 1.0],
    ]
    corr = _corr_df(labels, matrix)
    order = cluster_order(corr)
    assert set(order) == set(labels)
    idx = {s: i for i, s in enumerate(order)}
    assert abs(idx["A"] - idx["B"]) == 1
    assert abs(idx["C"] - idx["D"]) == 1


def test_cluster_order_deterministic():
    labels = ["W", "X", "Y", "Z"]
    matrix = [
        [1.0, 0.6, 0.1, -0.2],
        [0.6, 1.0, 0.3, -0.1],
        [0.1, 0.3, 1.0, 0.5],
        [-0.2, -0.1, 0.5, 1.0],
    ]
    corr = _corr_df(labels, matrix)
    assert cluster_order(corr) == cluster_order(corr)


def test_cluster_order_handles_nan_without_crashing():
    labels = ["A", "B", "C"]
    matrix = [
        [1.0, np.nan, 0.2],
        [np.nan, 1.0, 0.4],
        [0.2, 0.4, 1.0],
    ]
    corr = _corr_df(labels, matrix)
    order = cluster_order(corr)
    assert set(order) == set(labels)


# --- rank_other_side unit tests (pure helper, hand-built rows) ---


def _row(symbol, corr, ret, score):
    return OtherSideOut(symbol=symbol, corr=corr, ret=ret, score=score)


def test_rank_other_side_score_descending_is_primary():
    rows = [
        _row("LOW", -0.2, 0.01, 0.002),
        _row("HIGH", -0.5, 0.02, 0.010),
    ]
    assert [r.symbol for r in rank_other_side(rows)] == ["HIGH", "LOW"]


def test_rank_other_side_inverse_flat_outranks_uncorrelated_flat():
    # Both flat (ret ~ 0 -> clipped score 0), so the score alone can't
    # separate them — the secondary corr-ascending key must surface the
    # strongly-inverse-quiet name ("money hasn't rotated there YET") above
    # the uncorrelated-quiet one (fix-round-1 adjudication).
    rows = [
        _row("UNCORR_FLAT", 0.0, 0.0, 0.0),
        _row("INVERSE_FLAT", -0.8, 0.0, 0.0),
    ]
    assert [r.symbol for r in rank_other_side(rows)] == ["INVERSE_FLAT", "UNCORR_FLAT"]


def test_rank_other_side_positive_score_beats_any_zero_score_inverse():
    # An actually-moving negative-corr name still outranks even the most
    # inverse quiet one — the secondary key is a tie-break, not an override.
    rows = [
        _row("INVERSE_FLAT", -0.9, 0.0, 0.0),
        _row("MOVING", -0.3, 0.02, 0.006),
    ]
    assert [r.symbol for r in rank_other_side(rows)] == ["MOVING", "INVERSE_FLAT"]


def test_rank_other_side_null_score_and_corr_sink_last():
    rows = [
        _row("NULL_SCORE", None, None, None),
        _row("INVERSE_FLAT", -0.5, 0.0, 0.0),
        _row("MOVING", -0.4, 0.01, 0.004),
    ]
    assert [r.symbol for r in rank_other_side(rows)] == ["MOVING", "INVERSE_FLAT", "NULL_SCORE"]


def _crisis_store(tmp_path) -> BarStore:
    """SPY (benchmark) + a 2-name universe with ~260 days so a 20% tail has
    ~52 crisis days — enough for the crisis-correlation min-tail guard."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    rng = np.random.default_rng(3)
    n = 260
    factor = rng.normal(0.0, 0.012, n)  # market factor == SPY's driver
    a = 0.0005 + 0.6 * factor + rng.normal(0.0, 0.004, n)
    b = 0.0005 + 0.6 * factor + rng.normal(0.0, 0.004, n)
    for con_id, series in ((1, factor), (2, a), (3, b)):
        store.write_bars(con_id=con_id, bar_size="1d", bars=_bars_from_returns(series), meta=meta)
    store.write_symbol_map({"SPY": 1, "A": 2, "B": 3})
    return store


def test_rotation_crisis_returns_normal_and_crisis_matrices(tmp_path):
    client = _client(_crisis_store(tmp_path))
    r = client.post(
        "/api/rotation/crisis",
        json={"universe": "custom", "symbols": ["A", "B"], "tail": 0.2, "min_tail": 10, "years": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["benchmark"] == "SPY"
    assert body["symbols"] == sorted(["A", "B"]) or set(body["symbols"]) == {"A", "B"}
    assert len(body["normal_matrix"]) == 2 and len(body["crisis_matrix"]) == 2
    assert body["tail_n"] >= 10
    lo, hi = body["crisis_mean_corr_ci"]
    assert lo is not None and hi is not None and lo <= hi
    assert "range-restriction" in body["caveat"].lower()


def test_rotation_crisis_min_tail_guard_is_422(tmp_path):
    client = _client(_crisis_store(tmp_path))
    r = client.post(
        "/api/rotation/crisis",
        json={"universe": "custom", "symbols": ["A", "B"], "tail": 0.02, "min_tail": 20, "years": 5},
    )
    assert r.status_code == 422
    assert "min_tail" in r.json()["detail"]


def test_rotation_crisis_needs_two_instruments(tmp_path):
    client = _client(_crisis_store(tmp_path))
    r = client.post("/api/rotation/crisis", json={"universe": "custom", "symbols": ["A"]})
    assert r.status_code == 422


def test_rotation_crisis_rejects_a_future_dated_benchmark(tmp_path):
    store = _crisis_store(tmp_path)
    bars, meta = store.read_bars(con_id=1, bar_size="1d")
    future = pd.bdate_range(
        start=datetime.now(UTC).date() + timedelta(days=1), periods=1
    )[0].date()
    future_row = bars.iloc[[-1]].copy()
    future_row.index = pd.DatetimeIndex([future])
    store.write_bars(
        con_id=1,
        bar_size="1d",
        bars=pd.concat([bars, future_row]),
        meta=BarMeta(
            bar_type=meta.bar_type, adjusted_asof=future.isoformat()
        ),
    )

    response = _client(store).post(
        "/api/rotation/crisis",
        json={
            "universe": "custom",
            "symbols": ["A", "B"],
            "tail": 0.2,
            "min_tail": 10,
            "years": 5,
        },
    )

    assert response.status_code == 422
    assert "benchmark" in response.json()["detail"]
