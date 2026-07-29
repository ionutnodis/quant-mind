"""API contract tests for POST /api/whatif: hypothetical-book risk composition
reusing quantmind.risk.returns/montecarlo only (Global Constraints — routers
are thin composition, no math beyond that). Hypothetical books ARE the user's
book for color purposes (wave-2 constraints addendum) — this file only
asserts the numbers; amber rendering is web/src/pages/WhatIf.tsx's job.

Serialization policy: UTC ISO Z timestamps, NaN/Inf -> null, unknown symbols
or insufficient overlap -> structured 422, never a 500.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantmind.api.app import create_app
from quantmind.datastore.store import BarMeta, BarStore


def _bars(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    close = np.abs(np.cumprod(1 + rng.normal(0, 0.01, n))) * 100
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0}, index=idx
    )


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    # QQQ tracks SPY tick-for-tick in this fixture: a two-symbol book that's
    # economically ~100% index exposure has a hand-checkable beta ~= 1,
    # regardless of the 60/40 qty split between the two legs.
    store.write_bars(con_id=2, bar_size="1d", bars=spy_bars.copy(), meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def _payload(**overrides):
    body = {
        "positions": [{"symbol": "SPY", "qty": 60}, {"symbol": "QQQ", "qty": 40}],
        "years": 1,
        "mc": {"horizon": 21, "n_paths": 2000, "seed": 7},
    }
    body.update(overrides)
    return body


def test_whatif_two_symbol_book_beta_is_near_one(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["beta"] == pytest.approx(1.0, abs=1e-6)
    assert body["es_975"] is not None and body["es_975"] >= 0
    assert body["ann_vol"] is not None and body["ann_vol"] >= 0
    assert body["benchmark"]["symbol"] == "SPY"
    assert body["benchmark"]["es_975"] is not None
    assert body["benchmark"]["ann_vol"] is not None
    weights = {w["symbol"]: w for w in body["weights"]}
    assert weights["SPY"]["weight"] == pytest.approx(0.6)
    assert weights["QQQ"]["weight"] == pytest.approx(0.4)
    assert body["as_of"] and body["as_of"].endswith("Z")


def test_whatif_unknown_symbol_is_422_naming_it(client):
    r = client.post(
        "/api/whatif",
        json=_payload(positions=[{"symbol": "SPY", "qty": 1}, {"symbol": "NOPE", "qty": 1}]),
    )
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]


def test_whatif_qty_zero_is_422(client):
    r = client.post("/api/whatif", json=_payload(positions=[{"symbol": "SPY", "qty": 0}]))
    assert r.status_code == 422


def test_whatif_too_many_positions_is_422(client):
    positions = [{"symbol": "SPY", "qty": 1} for _ in range(51)]
    r = client.post("/api/whatif", json=_payload(positions=positions))
    assert r.status_code == 422


def test_whatif_empty_positions_is_422(client):
    r = client.post("/api/whatif", json=_payload(positions=[]))
    assert r.status_code == 422


def test_whatif_years_bounds_reject_out_of_range(client):
    r = client.post("/api/whatif", json=_payload(years=0))
    assert r.status_code == 422
    r2 = client.post("/api/whatif", json=_payload(years=100))
    assert r2.status_code == 422


def test_whatif_mc_bounds_reject_resource_exhaustion(client):
    r = client.post(
        "/api/whatif", json=_payload(mc={"horizon": 21, "n_paths": 10_000_000, "seed": 1})
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/whatif", json=_payload(mc={"horizon": 100_000, "n_paths": 100, "seed": 1})
    )
    assert r2.status_code == 422


def test_whatif_mc_seeded_run_is_reproducible(client):
    payload = _payload()
    r1 = client.post("/api/whatif", json=payload)
    r2 = client.post("/api/whatif", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_whatif_returns_mc_histogram_and_percentiles(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    body = r.json()
    hist = body["mc"]["histogram"]
    assert len(hist["bin_edges"]) == len(hist["counts"]) + 1
    assert len(hist["counts"]) <= 60
    assert sum(hist["counts"]) == 2000 - body["mc"]["n_nonfinite"]
    assert body["mc"]["p5"] <= body["mc"]["p50"] <= body["mc"]["p95"]


def test_whatif_insufficient_overlap_is_422(tmp_path):
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    short_bars = _bars(n=10, seed=3)
    store.write_bars(con_id=1, bar_size="1d", bars=short_bars, meta=meta)
    store.write_symbol_map({"SPY": 1})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    short_client = TestClient(
        app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"}
    )
    r = short_client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 1}],
            "years": 1,
            "mc": {"horizon": 5, "n_paths": 100},
        },
    )
    assert r.status_code == 422
    assert "detail" in r.json()


def test_whatif_nonfinite_last_close_is_422_naming_the_symbol(tmp_path):
    # Corrupted/partial sync data: a NaN last close makes the leg unpriceable.
    # Unlike GET /api/portfolio (display of broker truth, where a leg degrades
    # to null fields), What-If's weights ARE the risk computation — silently
    # dropping the leg would compute risk for a different book than the one
    # the user built. So: structured 422 naming the symbol, never a NaN
    # leaking into the JSON (binding NaN/Inf -> null / never-crash policy).
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    store.write_bars(con_id=1, bar_size="1d", bars=_bars(seed=1), meta=meta)
    bad_bars = _bars(seed=2)
    bad_bars.loc[bad_bars.index[-1], "close"] = np.nan
    store.write_bars(con_id=2, bar_size="1d", bars=bad_bars, meta=meta)
    store.write_symbol_map({"SPY": 1, "QQQ": 2})
    app = create_app(store=store, benchmark="SPY", api_token="testtoken")
    c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})

    r = c.post("/api/whatif", json=_payload())
    assert r.status_code == 422
    assert "QQQ" in r.json()["detail"]

    # A book that avoids the corrupted symbol still computes fine.
    r2 = c.post("/api/whatif", json=_payload(positions=[{"symbol": "SPY", "qty": 10}]))
    assert r2.status_code == 200


def test_whatif_single_position_defaults_mc(client):
    r = client.post(
        "/api/whatif",
        json={"positions": [{"symbol": "SPY", "qty": 10}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["weights"][0]["weight"] == pytest.approx(1.0)
    assert body["mc"]["histogram"]["counts"]


# --- book_ref (wave-3 Task A1's book-flow spine): an alternative to inline
# `positions`, resolved via routers/book.py's pinned snapshots. ---


def test_whatif_book_ref_resolves_to_the_same_result_as_inline_positions(client):
    pinned = client.post(
        "/api/book/pin", json={"positions": [{"symbol": "SPY", "qty": 60}, {"symbol": "QQQ", "qty": 40}]}
    ).json()

    r_ref = client.post("/api/whatif", json={"book_ref": pinned["snapshot_id"], "years": 1, "mc": {"horizon": 21, "n_paths": 2000, "seed": 7}})
    r_inline = client.post("/api/whatif", json=_payload())
    assert r_ref.status_code == r_inline.status_code == 200
    assert r_ref.json() == r_inline.json()


def test_whatif_unknown_book_ref_is_422(client):
    r = client.post("/api/whatif", json={"book_ref": "does-not-exist", "years": 1})
    assert r.status_code == 422
    assert "does-not-exist" in r.json()["detail"]


def test_whatif_both_positions_and_book_ref_is_422(client):
    r = client.post("/api/whatif", json=_payload(book_ref="whatever"))
    assert r.status_code == 422


def test_whatif_neither_positions_nor_book_ref_is_422(client):
    r = client.post("/api/whatif", json={"years": 1})
    assert r.status_code == 422


# --- wave-3B What-If flow: base_book_ref diff (trade ticket), common-random-
# numbers paired sims (shared seed exposed in the response), option legs
# through BOTH the inline-positions and book_ref paths. ---


def _pin(client, positions):
    r = client.post("/api/book/pin", json={"positions": positions})
    assert r.status_code == 200
    return r.json()


def test_whatif_response_echoes_shared_seed_and_horizon(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    mc = r.json()["mc"]
    assert mc["seed"] == 7
    assert mc["horizon_days"] == 21


def test_whatif_generates_and_echoes_seed_when_none_given(client):
    # No seed in the request: the router must draw ONE shared seed, use it,
    # and expose it — replaying with the echoed seed reproduces the
    # distribution exactly (the CRN plumbing made auditable).
    r = client.post("/api/whatif", json=_payload(mc={"horizon": 21, "n_paths": 500}))
    assert r.status_code == 200
    seed = r.json()["mc"]["seed"]
    assert isinstance(seed, int)
    r2 = client.post("/api/whatif", json=_payload(mc={"horizon": 21, "n_paths": 500, "seed": seed}))
    assert r2.status_code == 200
    assert r2.json()["mc"]["histogram"] == r.json()["mc"]["histogram"]


def test_whatif_crn_identity_delta_is_exactly_zero_for_identical_books(client):
    # The CRN identity: base and hypothetical books simulate on the SAME
    # bootstrap draws, so an identical book yields a delta of EXACTLY zero
    # (float a - a == 0.0), not merely approximately — no seed is posted on
    # purpose, the shared generated seed alone must guarantee pairing.
    pinned = _pin(client, [{"symbol": "SPY", "qty": 60}, {"symbol": "QQQ", "qty": 40}])
    r = client.post(
        "/api/whatif",
        json={
            "book_ref": pinned["snapshot_id"],
            "base_book_ref": pinned["snapshot_id"],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 2000},
        },
    )
    assert r.status_code == 200
    body = r.json()
    delta = body["delta"]
    assert delta["beta"] == 0.0
    assert delta["es_975"] == 0.0
    assert delta["ann_vol"] == 0.0
    assert delta["p5"] == 0.0
    assert delta["p50"] == 0.0
    assert delta["p95"] == 0.0
    assert body["trade_ticket"] == []
    base = body["base"]
    assert base["book_ref"] == pinned["snapshot_id"]
    assert base["es_975"] == body["es_975"]
    assert base["valuation_ts"] and base["valuation_ts"].endswith("Z")


def test_whatif_trade_ticket_diffs_current_to_hypothetical(client):
    pinned = _pin(client, [{"symbol": "SPY", "qty": 100}])
    r = client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 150}, {"symbol": "QQQ", "qty": -10}],
            "base_book_ref": pinned["snapshot_id"],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 200
    ticket = {t["symbol"]: t for t in r.json()["trade_ticket"]}
    assert ticket["SPY"]["qty_from"] == 100
    assert ticket["SPY"]["qty_to"] == 150
    assert ticket["SPY"]["qty_delta"] == 50
    assert ticket["SPY"]["action"] == "BUY"
    assert ticket["SPY"]["sec_type"] == "STK"
    assert ticket["SPY"]["price"] is not None
    assert ticket["QQQ"]["qty_from"] == 0
    assert ticket["QQQ"]["qty_to"] == -10
    assert ticket["QQQ"]["qty_delta"] == -10
    assert ticket["QQQ"]["action"] == "SELL"


def test_whatif_no_base_book_ref_means_no_base_delta_or_ticket(client):
    r = client.post("/api/whatif", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["base"] is None
    assert body["delta"] is None
    assert body["trade_ticket"] is None


def test_whatif_unknown_base_book_ref_is_422(client):
    r = client.post("/api/whatif", json=_payload(base_book_ref="ffffffffffff"))
    assert r.status_code == 422
    assert "ffffffffffff" in r.json()["detail"]


def test_whatif_empty_base_book_yields_null_base_risk_and_all_open_ticket(client):
    # No broker configured in tests: pinning without positions pins an EMPTY
    # live book. The diff against it is still honest — no base risk numbers
    # (there is no base book to price) and a ticket that opens every leg.
    pinned = client.post("/api/book/pin", json={}).json()
    r = client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 10}],
            "base_book_ref": pinned["snapshot_id"],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["base"]["es_975"] is None
    assert body["base"]["n_positions"] == 0
    assert body["delta"] is None
    assert [t["action"] for t in body["trade_ticket"]] == ["BUY"]
    assert body["trade_ticket"][0]["qty_from"] == 0
    assert body["trade_ticket"][0]["qty_to"] == 10


def test_whatif_option_leg_inline_prices_at_multiplier_scaled_notional(client):
    # Delta-one proxy (declared in `notes`, never silent): 1 call contract at
    # multiplier 100 carries the same underlier notional as 100 shares, so
    # this book weighs 50/50 — and the option leg's descriptor fields echo
    # back on its weight row.
    r = client.post(
        "/api/whatif",
        json={
            "positions": [
                {"symbol": "SPY", "qty": 100},
                {"symbol": "SPY", "qty": 1, "strike": 400, "expiry": "2026-09-18", "right": "C"},
            ],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["weights"][0]["weight"] == pytest.approx(0.5)
    assert body["weights"][1]["weight"] == pytest.approx(0.5)
    assert body["weights"][1]["sec_type"] == "OPT"
    assert body["weights"][1]["strike"] == 400
    assert body["weights"][1]["expiry"] == "20260918"
    assert body["weights"][1]["right"] == "C"
    assert body["weights"][1]["multiplier"] == 100
    assert body["weights"][0]["sec_type"] == "STK"
    assert body["notes"]  # the delta-one approximation is declared


def test_whatif_option_leg_via_book_ref_matches_inline(client):
    positions = [
        {"symbol": "SPY", "qty": 100},
        {"symbol": "SPY", "qty": 1, "strike": 400, "expiry": "20260918", "right": "C"},
    ]
    pinned = _pin(client, positions)
    mc = {"horizon": 21, "n_paths": 500, "seed": 7}
    r_ref = client.post(
        "/api/whatif", json={"book_ref": pinned["snapshot_id"], "years": 1, "mc": mc}
    )
    r_inline = client.post("/api/whatif", json={"positions": positions, "years": 1, "mc": mc})
    assert r_ref.status_code == r_inline.status_code == 200
    assert r_ref.json()["weights"] == r_inline.json()["weights"]
    assert r_ref.json()["mc"] == r_inline.json()["mc"]


def test_whatif_inline_option_leg_missing_strike_expiry_is_422(client):
    # Fix round 1 (I1): the inline path must refuse the same incomplete OPT
    # leg the book_ref path refuses (book.py's honest-refusal guard) —
    # previously this returned 200, silently priced at 100x underlier notional.
    r = client.post(
        "/api/whatif",
        json=_payload(positions=[{"symbol": "SPY", "qty": 1, "right": "C"}]),
    )
    assert r.status_code == 422
    assert "SPY" in r.json()["detail"]
    assert "strike" in r.json()["detail"]


def test_whatif_partial_option_descriptor_without_right_is_422(client):
    # Fix round 1 (I1 + reviewer minor 4): strike/expiry/right are
    # all-or-none — a leg with strike but no right previously keyed as a
    # phantom separate STK line in the trade ticket.
    r = client.post(
        "/api/whatif",
        json=_payload(positions=[{"symbol": "SPY", "qty": 1, "strike": 400, "expiry": "20260918"}]),
    )
    assert r.status_code == 422
    assert "SPY" in r.json()["detail"]
    assert "together" in r.json()["detail"]


def test_whatif_partial_option_descriptor_is_now_refused_at_pin_time(client):
    # Batch-2 final review, item 1: /api/book/pin used to happily persist a
    # strike-without-right leg (as STK sec_type, so book.py's OPT guard never
    # fired). The all-or-none guard now runs AT PIN TIME.
    r = client.post(
        "/api/book/pin",
        json={"positions": [{"symbol": "SPY", "qty": 1, "strike": 400, "expiry": "20260918"}]},
    )
    assert r.status_code == 422
    assert "together" in r.json()["detail"]


def test_whatif_legacy_partial_snapshot_on_disk_is_422_on_both_ref_paths(client):
    # A PRE-FIX snapshot already on disk with a partial descriptor must still
    # be refused at resolution time on BOTH the hypothetical and base paths
    # (book.py's read guard covers legacy files the pin guard predates).
    import json as _json

    from quantmind.api.routers.book import _books_dir

    store = client.app.state.store  # type: ignore[attr-defined]
    ref = "aaaaaaaaaaaa"
    payload = {
        "snapshot_id": ref,
        "valuation_ts": "2026-07-24T00:00:00Z",
        "base_currency": "USD",
        "positions": [
            {"con_id": 1, "symbol": "SPY", "qty": 1, "sec_type": "STK",
             "multiplier": 1.0, "strike": 400.0, "expiry": "20260918", "right": None}
        ],
    }
    (_books_dir(store) / f"{ref}.json").write_text(_json.dumps(payload))

    r = client.post(
        "/api/whatif",
        json={"book_ref": ref, "years": 1, "mc": {"horizon": 21, "n_paths": 500, "seed": 7}},
    )
    assert r.status_code == 422
    assert "re-pin with explicit legs" in r.json()["detail"]

    r_base = client.post("/api/whatif", json=_payload(base_book_ref=ref))
    assert r_base.status_code == 422
    assert "re-pin with explicit legs" in r_base.json()["detail"]


def test_whatif_option_leg_in_trade_ticket_keys_on_the_full_leg(client):
    # Base holds the underlier only; hypothetical adds a call overlay. The
    # ticket must key legs on (symbol, strike, expiry, right, multiplier) —
    # the option leg is a NEW line, not a qty change on the stock line.
    pinned = _pin(client, [{"symbol": "SPY", "qty": 100}])
    r = client.post(
        "/api/whatif",
        json={
            "positions": [
                {"symbol": "SPY", "qty": 100},
                {"symbol": "SPY", "qty": -2, "strike": 380, "expiry": "20260918", "right": "P"},
            ],
            "base_book_ref": pinned["snapshot_id"],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 200
    ticket = r.json()["trade_ticket"]
    assert len(ticket) == 1  # the unchanged 100-share stock line is absent
    (line,) = ticket
    assert line["sec_type"] == "OPT"
    assert line["strike"] == 380
    assert line["right"] == "P"
    assert line["qty_delta"] == -2
    assert line["action"] == "SELL"
    assert line["multiplier"] == 100


# --- FX-aware valuation: market values/weights in the base currency ---


def _two_currency_whatif_client(tmp_path, with_rate=True):
    """SPY (USD) + LSEQ (GBP-quoted, tick-for-tick SPY clone); base USD,
    GBPUSD cached at 1.25 — the FX bias in weights is the thing under test."""
    store = BarStore(tmp_path)
    meta = BarMeta(bar_type="ADJUSTED_LAST", adjusted_asof="2026-07-24")
    spy_bars = _bars(seed=1)
    store.write_bars(con_id=1, bar_size="1d", bars=spy_bars, meta=meta)
    store.write_bars(con_id=2, bar_size="1d", bars=spy_bars.copy(), meta=meta)
    store.write_symbol_map({"SPY": 1, "LSEQ": 2})
    store.write_instrument_metadata("SPY", {"con_id": 1, "currency": "USD"})
    store.write_instrument_metadata("LSEQ", {"con_id": 2, "currency": "GBP"})
    if with_rate:
        idx = pd.bdate_range(end="2026-07-24", periods=2)
        store.write_series("FX_GBPUSD", pd.Series([1.2, 1.25], index=idx))
    app = create_app(store=store, benchmark="SPY", api_token="testtoken", base_currency="USD")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})
    return client, spy_bars


def test_whatif_two_currency_weights_convert_market_values(tmp_path):
    # Hand-computed: last close L for both legs. SPY mv = 10L (USD);
    # LSEQ mv = 5L x 1.25 = 6.25L (GBP->USD). Gross 16.25L; weights
    # 10/16.25 and 6.25/16.25. Prices stay NATIVE per-share (L for both).
    client, spy_bars = _two_currency_whatif_client(tmp_path)
    last = float(spy_bars["close"].iloc[-1])
    r = client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 10}, {"symbol": "LSEQ", "qty": 5}],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 200
    body = r.json()
    w = {row["symbol"]: row for row in body["weights"]}
    assert w["SPY"]["price"] == pytest.approx(last, rel=1e-9)
    assert w["LSEQ"]["price"] == pytest.approx(last, rel=1e-9)  # native quote
    assert w["SPY"]["market_value"] == pytest.approx(10 * last, rel=1e-9)
    assert w["LSEQ"]["market_value"] == pytest.approx(5 * last * 1.25, rel=1e-9)
    assert w["SPY"]["weight"] == pytest.approx(10.0 / 16.25, rel=1e-9)
    assert w["LSEQ"]["weight"] == pytest.approx(6.25 / 16.25, rel=1e-9)
    assert any("GBPUSD" in n and "valued in USD" in n for n in body["notes"])


def test_whatif_missing_fx_rate_is_named_422(tmp_path):
    client, _ = _two_currency_whatif_client(tmp_path, with_rate=False)
    r = client.post(
        "/api/whatif",
        json={
            "positions": [{"symbol": "SPY", "qty": 10}, {"symbol": "LSEQ", "qty": 5}],
            "years": 1,
            "mc": {"horizon": 21, "n_paths": 500, "seed": 7},
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "GBP" in detail and "LSEQ" in detail and "sync" in detail
