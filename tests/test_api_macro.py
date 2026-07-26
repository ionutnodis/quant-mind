"""API contract tests for GET /api/macro: yields/curve, Fed net liquidity,
sector & factor rotation — all from the store, never network, never a 500.

Serialization policy (repo-wide): NaN/Inf -> null, missing series/bars ->
that block omitted (or that symbol dropped) and named in `missing`, never a
500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.api.routers.macro import FACTORS, SECTORS
from quantmind.datastore.store import BarMeta, BarStore

# Deterministic per-symbol daily drift so ret_1d/1m/3m are hand-checkable
# ((1+drift)^k - 1) and the resulting sort order is known up front.
SECTOR_DRIFT = {
    "XLK": 0.010, "XLY": 0.008, "XLF": 0.005, "XLB": 0.003, "XLV": 0.002,
    "XLI": 0.000, "XLP": -0.002, "XLU": -0.005, "XLE": -0.010,
}
FACTOR_DRIFT = {"MTUM": 0.006, "QUAL": 0.001, "USMV": -0.001, "VLUE": -0.004}

N_BARS = 70  # > 63 trading days so ret_3m is computable


def _drift_bars(drift: float, n: int = N_BARS) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = 100.0 * (1.0 + drift) ** np.arange(n)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _flat_series(values: list[float], end: str = "2026-07-24") -> pd.Series:
    idx = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=idx)


def _level_bars(values: np.ndarray) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-24", periods=len(values))
    close = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


def _write_universe(store: BarStore, include_vix: bool = True) -> dict[str, int]:
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    symbol_map: dict[str, int] = {}
    con_id = 1
    for symbol, drift in {**SECTOR_DRIFT, **FACTOR_DRIFT}.items():
        store.write_bars(con_id=con_id, bar_size="1d", bars=_drift_bars(drift), meta=meta)
        symbol_map[symbol] = con_id
        con_id += 1
    if include_vix:
        # A steadily rising VIX (15.0, 15.1, ...) so tercile membership is a
        # hand-known time split (first/middle/last thirds of the sample).
        vix_meta = BarMeta(bar_type="TRADES", adjusted_asof="2026-07-24")
        store.write_bars(
            con_id=900, bar_size="1d", bars=_level_bars(15.0 + 0.1 * np.arange(N_BARS)), meta=vix_meta
        )
        symbol_map["VIX"] = 900
    store.write_symbol_map(symbol_map)
    return symbol_map


def _write_yields(store: BarStore) -> None:
    # 69 flat points + a final value: the LATEST is a known hand-case number,
    # while the 21-trading-day (m1) and 63-trading-day (m3) curve snapshots
    # both land on the flat 0.040/0.030/0.050 stretch.
    store.write_series("US10Y", _flat_series([0.040] * 69 + [0.045]))
    store.write_series("US2Y", _flat_series([0.030] * 69 + [0.038]))
    store.write_series("US3M", _flat_series([0.050] * 69 + [0.052]))


def _write_net_liquidity(store: BarStore) -> None:
    store.write_series("NET_LIQUIDITY", _flat_series([6000.0] * 10 + [6100.0]))


@pytest.fixture
def full_store(tmp_path) -> BarStore:
    store = BarStore(tmp_path)
    _write_yields(store)
    _write_net_liquidity(store)
    _write_universe(store)
    return store


@pytest.fixture
def partial_store_no_net_liquidity(tmp_path) -> BarStore:
    store = BarStore(tmp_path)
    _write_yields(store)
    _write_universe(store)
    return store


@pytest.fixture
def empty_store(tmp_path) -> BarStore:
    return BarStore(tmp_path)


def _client(store: BarStore) -> TestClient:
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_macro_full_store_shape_and_spread_arithmetic(full_store):
    r = _client(full_store).get("/api/macro")
    assert r.status_code == 200
    body = r.json()

    assert body["missing"] == []
    assert body["as_of"] is not None and body["as_of"].endswith("Z")

    y = body["yields"]
    assert y["us10y"] == pytest.approx(0.045)
    assert y["us2y"] == pytest.approx(0.038)
    assert y["us3m"] == pytest.approx(0.052)
    assert y["spread_2s10s"] == pytest.approx(0.045 - 0.038)
    assert set(y["series"].keys()) == {"us10y", "us2y", "us3m"}
    assert len(y["series"]["us10y"]) <= 500
    assert y["series"]["us10y"][-1]["value"] == pytest.approx(0.045)

    nl = body["net_liquidity"]
    assert nl["latest_bn"] == pytest.approx(6100.0)
    assert nl["cadence_note"] == "weekly"
    assert len(nl["series"]) <= 500

    sectors = body["sectors"]
    assert [row["symbol"] for row in sectors] == sorted(SECTORS, key=lambda s: -SECTOR_DRIFT[s])
    xlk = next(row for row in sectors if row["symbol"] == "XLK")
    assert xlk["ret_1d"] == pytest.approx(0.010, abs=1e-6)
    assert xlk["ret_1m"] == pytest.approx((1.010) ** 21 - 1, rel=1e-6)
    assert xlk["ret_3m"] == pytest.approx((1.010) ** 63 - 1, rel=1e-6)

    factors = body["factors"]
    assert [row["symbol"] for row in factors] == sorted(FACTORS, key=lambda s: -FACTOR_DRIFT[s])


def test_macro_partial_store_omits_net_liquidity_block(partial_store_no_net_liquidity):
    r = _client(partial_store_no_net_liquidity).get("/api/macro")
    assert r.status_code == 200
    body = r.json()

    assert body["net_liquidity"] is None
    assert "NET_LIQUIDITY" in body["missing"]
    # everything else still present
    assert body["yields"] is not None
    assert len(body["sectors"]) == len(SECTORS)
    assert len(body["factors"]) == len(FACTORS)


def test_macro_empty_store_is_200_all_missing(empty_store):
    r = _client(empty_store).get("/api/macro")
    assert r.status_code == 200
    body = r.json()

    assert body["yields"] is None
    assert body["net_liquidity"] is None
    assert body["sectors"] == []
    assert body["factors"] == []
    assert body["as_of"] is None
    # wave-3B blocks degrade to structured nulls too, never a 500.
    assert body["curve"] is None
    assert body["regime_rotation"] is None
    assert body["sensitivity"] is None

    missing = set(body["missing"])
    assert {"US10Y", "US2Y", "US3M", "NET_LIQUIDITY", "VIX"} <= missing
    assert set(SECTORS) <= missing
    assert set(FACTORS) <= missing


def test_macro_mapped_symbol_without_bars_is_skipped_not_500(tmp_path):
    store = BarStore(tmp_path)
    _write_yields(store)
    _write_net_liquidity(store)
    # GHOST is mapped but was never synced (no cached bars at any bar size).
    store.write_symbol_map({"XLK": 1, "GHOST_SECTOR": 99})
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_drift_bars(0.01), meta=meta)

    r = _client(store).get("/api/macro")
    assert r.status_code == 200
    body = r.json()
    assert [row["symbol"] for row in body["sectors"]] == ["XLK"]
    assert set(SECTORS) - {"XLK"} <= set(body["missing"])


# --- wave-3B: curve snapshots ----------------------------------------------


def test_macro_curve_snapshots_today_vs_1m_3m(full_store):
    body = _client(full_store).get("/api/macro").json()
    curve = body["curve"]
    assert curve is not None
    assert [t["tenor"] for t in curve["tenors"]] == ["US3M", "US2Y", "US10Y"]
    tenors = {t["tenor"]: t for t in curve["tenors"]}

    assert tenors["US10Y"]["years"] == pytest.approx(10.0)
    assert tenors["US3M"]["years"] == pytest.approx(0.25)
    # today = the final hand-case value; 21d/63d-ago land on the flat stretch.
    assert tenors["US10Y"]["today"] == pytest.approx(0.045)
    assert tenors["US10Y"]["m1"] == pytest.approx(0.040)
    assert tenors["US10Y"]["m3"] == pytest.approx(0.040)
    assert tenors["US2Y"]["today"] == pytest.approx(0.038)
    assert tenors["US2Y"]["m1"] == pytest.approx(0.030)

    assert curve["spread_2s10s_today"] == pytest.approx(0.045 - 0.038)
    assert curve["spread_2s10s_m1"] == pytest.approx(0.010)
    assert curve["spread_2s10s_m3"] == pytest.approx(0.010)
    # snapshot horizons are labeled (Global Constraint: every horizon labeled)
    assert "21" in curve["note"] and "63" in curve["note"]


def test_macro_curve_short_history_m3_is_null(tmp_path):
    store = BarStore(tmp_path)
    store.write_series("US10Y", _flat_series([0.040] * 29 + [0.045]))
    store.write_series("US2Y", _flat_series([0.030] * 29 + [0.038]))
    store.write_series("US3M", _flat_series([0.050] * 29 + [0.052]))
    body = _client(store).get("/api/macro").json()
    tenors = {t["tenor"]: t for t in body["curve"]["tenors"]}
    assert tenors["US10Y"]["m1"] == pytest.approx(0.040)  # 30 points > 21-day lag
    assert tenors["US10Y"]["m3"] is None  # not enough history: null, not a 500
    assert body["curve"]["spread_2s10s_m3"] is None


# --- wave-3B: regime-conditional rotation ----------------------------------


def test_macro_regime_rotation_vix_terciles(full_store):
    body = _client(full_store).get("/api/macro").json()
    reg = body["regime_rotation"]
    assert reg is not None
    assert "VIX" in reg["regime_note"] and "tercile" in reg["regime_note"].lower()

    buckets = reg["buckets"]
    assert [b["bucket"] for b in buckets] == ["low", "mid", "high"]
    assert sum(b["n_days"] for b in buckets) == N_BARS - 1  # aligned return days
    for b in buckets:
        symbols = [row["symbol"] for row in b["rows"]]
        assert set(symbols) == set(SECTORS) | set(FACTORS)
        # rows ranked by mean daily return desc: XLK (drift +1.0%) leads
        assert symbols[0] == "XLK"
        xlk = b["rows"][0]
        # constant-drift bars: mean is the drift exactly, SE exactly 0
        assert xlk["mean_daily"] == pytest.approx(0.010, abs=1e-9)
        assert xlk["se_daily"] == pytest.approx(0.0, abs=1e-12)
        # regime bounds + bucket size are exposed (honest labeling)
        assert b["lo"] is not None and b["hi"] is not None and b["n_days"] > 0
    assert reg["as_of"] is not None and reg["as_of"].endswith("Z")


def test_macro_without_vix_regime_block_is_null(tmp_path):
    store = BarStore(tmp_path)
    _write_yields(store)
    _write_net_liquidity(store)
    _write_universe(store, include_vix=False)
    body = _client(store).get("/api/macro").json()
    assert body["regime_rotation"] is None
    assert "VIX" in body["missing"]
    # the rest of the page is unaffected
    assert body["yields"] is not None
    assert len(body["sectors"]) == len(SECTORS)


# --- wave-3B: book sensitivity column --------------------------------------


@pytest.fixture
def noisy_store(tmp_path) -> BarStore:
    """Random-walk closes/yields (seeded) so sensitivity regressions have
    non-degenerate variance — the drift fixtures are constant-return by
    construction and exist for the rotation hand-cases."""
    rng = np.random.default_rng(7)
    store = BarStore(tmp_path)
    n = 300
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    symbol_map: dict[str, int] = {}
    con_id = 1
    for symbol in list(SECTOR_DRIFT) + list(FACTOR_DRIFT):
        close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
        store.write_bars(con_id=con_id, bar_size="1d", bars=_level_bars(close), meta=meta)
        symbol_map[symbol] = con_id
        con_id += 1
    vix = np.clip(18.0 + np.cumsum(rng.normal(0.0, 0.3, n)), 9.0, None)
    store.write_bars(
        con_id=900, bar_size="1d", bars=_level_bars(vix),
        meta=BarMeta(bar_type="TRADES", adjusted_asof="2026-07-24"),
    )
    symbol_map["VIX"] = 900
    store.write_symbol_map(symbol_map)
    for name, start in [("US10Y", 0.045), ("US2Y", 0.040), ("US3M", 0.050)]:
        levels = start + np.cumsum(rng.normal(0.0, 2e-4, n))
        store.write_series(name, pd.Series(levels, index=idx))
    store.write_series("NET_LIQUIDITY", _flat_series([6000.0] * 10 + [6100.0]))
    return store


def test_macro_without_book_ref_sensitivity_is_null(full_store):
    body = _client(full_store).get("/api/macro").json()
    assert body["sensitivity"] is None  # frontend: "pin a book to see sensitivities"


def test_macro_unknown_book_ref_is_422(full_store):
    r = _client(full_store).get("/api/macro", params={"book_ref": "abcdefabcdef"})
    assert r.status_code == 422


def test_macro_malformed_book_ref_is_422_not_path_traversal(full_store):
    r = _client(full_store).get("/api/macro", params={"book_ref": "../instrument"})
    assert r.status_code == 422


def test_macro_sensitivity_with_pinned_book(noisy_store):
    client = _client(noisy_store)
    pin = client.post("/api/book/pin", json={"positions": [{"symbol": "XLK", "qty": 10}]})
    assert pin.status_code == 200
    ref = pin.json()["snapshot_id"]

    r = client.get("/api/macro", params={"book_ref": ref})
    assert r.status_code == 200
    sens = r.json()["sensitivity"]
    assert sens is not None
    assert sens["book_ref"] == ref

    con_id = noisy_store.read_symbol_map()["XLK"]
    bars, _ = noisy_store.read_bars(con_id=con_id, bar_size="1d")
    gross = 10.0 * float(bars["close"].iloc[-1])
    assert sens["book_gross"] == pytest.approx(gross, rel=1e-9)

    rows = {(row["group"], row["driver"]): row for row in sens["rows"]}

    # A 100% XLK book regressed on XLK itself: beta exactly 1, so the +1%
    # shock response is exactly 1% of gross with a degenerate (zero-width) CI.
    xlk = rows[("sectors", "XLK")]
    assert xlk["shock_label"] == "+1%"
    assert xlk["dollar_response"] == pytest.approx(0.01 * gross, rel=1e-6)
    assert xlk["ci_low"] == pytest.approx(xlk["dollar_response"], rel=1e-4)
    assert xlk["ci_high"] == pytest.approx(xlk["dollar_response"], rel=1e-4)
    assert xlk["n_obs"] == 252  # regression window labeled AND enforced

    us10y = rows[("rates", "US10Y")]
    assert us10y["shock_label"] == "+10bp"
    assert us10y["dollar_response"] is not None
    assert us10y["ci_low"] <= us10y["dollar_response"] <= us10y["ci_high"]
    assert us10y["se"] is not None and us10y["se"] > 0

    assert ("rates", "US2Y") in rows
    vix = rows[("vol", "VIX")]
    assert vix["shock_label"] == "+5 vol pts"
    assert vix["ci_low"] <= vix["dollar_response"] <= vix["ci_high"]

    # every sector/factor row has a sensitivity entry
    for symbol in SECTOR_DRIFT:
        assert ("sectors", symbol) in rows
    for symbol in FACTOR_DRIFT:
        assert ("factors", symbol) in rows

    # horizon labeling (Global Constraint): window + estimator named
    assert "252" in sens["window_note"]
    assert "HAC" in sens["window_note"] or "Newey" in sens["window_note"]
    assert sens["as_of"] is not None and sens["as_of"].endswith("Z")


def test_macro_sensitivity_option_leg_excluded_honestly(noisy_store):
    client = _client(noisy_store)
    pin = client.post(
        "/api/book/pin",
        json={
            "positions": [
                {"symbol": "XLF", "qty": 5},
                {"symbol": "XLK", "qty": 1, "right": "C", "strike": 100, "expiry": "2026-12-18"},
            ]
        },
    )
    assert pin.status_code == 200
    ref = pin.json()["snapshot_id"]

    sens = client.get("/api/macro", params={"book_ref": ref}).json()["sensitivity"]
    assert sens is not None
    assert any("XLK" in e for e in sens["excluded"])  # option leg named, not silently priced

    con_id = noisy_store.read_symbol_map()["XLF"]
    bars, _ = noisy_store.read_bars(con_id=con_id, bar_size="1d")
    assert sens["book_gross"] == pytest.approx(5.0 * float(bars["close"].iloc[-1]), rel=1e-9)


def test_macro_sensitivity_degenerate_book_never_500_and_never_ci_free(full_store):
    # full_store's drift bars are constant-return: the book's return variance
    # is pure float noise. The page must still render (never a 500), and the
    # CI/SE law must hold row by row: an estimate is EITHER served with its
    # full SE + CI, OR refused (nulls + a note naming why) — never a bare
    # number whose uncertainty came back NaN.
    client = _client(full_store)
    ref = client.post("/api/book/pin", json={"positions": [{"symbol": "XLK", "qty": 10}]}).json()[
        "snapshot_id"
    ]
    r = client.get("/api/macro", params={"book_ref": ref})
    assert r.status_code == 200
    sens = r.json()["sensitivity"]
    assert sens is not None
    assert len(sens["rows"]) > 0
    for row in sens["rows"]:
        if row["dollar_response"] is None:
            assert row["note"]  # refused rows name why the estimate is unavailable
        else:
            assert row["se"] is not None
            assert row["ci_low"] is not None and row["ci_high"] is not None
    # the VIX row's HAC covariance is NaN on this degenerate book -> refused
    vix = next(r for r in sens["rows"] if r["driver"] == "VIX")
    assert vix["dollar_response"] is None and vix["note"]
