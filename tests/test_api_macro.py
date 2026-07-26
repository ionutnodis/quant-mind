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


def _write_universe(store: BarStore) -> dict[str, int]:
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    symbol_map: dict[str, int] = {}
    con_id = 1
    for symbol, drift in {**SECTOR_DRIFT, **FACTOR_DRIFT}.items():
        store.write_bars(con_id=con_id, bar_size="1d", bars=_drift_bars(drift), meta=meta)
        symbol_map[symbol] = con_id
        con_id += 1
    store.write_symbol_map(symbol_map)
    return symbol_map


def _write_yields(store: BarStore) -> None:
    # 29 flat points + a final value so the *latest* is a known hand-case number.
    store.write_series("US10Y", _flat_series([0.040] * 29 + [0.045]))
    store.write_series("US2Y", _flat_series([0.030] * 29 + [0.038]))
    store.write_series("US3M", _flat_series([0.050] * 29 + [0.052]))


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

    missing = set(body["missing"])
    assert {"US10Y", "US2Y", "US3M", "NET_LIQUIDITY"} <= missing
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
