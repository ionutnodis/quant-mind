from __future__ import annotations

from datetime import UTC, datetime

from quantmind.datastore.store import BarStore
from quantmind.sources.providers.justetf import JustEtfProvider
from quantmind.sources.ucits_sync import sync_ucits_profiles


PROFILE_HTML = """
<html><h1>iShares Core MSCI World UCITS ETF USD (Acc)</h1>
<table>
  <tr><td data-testid="tl_etf-basics_value_isin">IE00B4L5Y983</td></tr>
  <tr><td data-testid="tl_etf-basics_value_index-name">MSCI World</td></tr>
  <tr><td data-testid="tl_etf-basics_value_ter">0.20% p.a.</td></tr>
  <tr><td data-testid="tl_etf-basics_value_replication">Physical</td></tr>
  <tr><td data-testid="tl_etf-basics_value_distribution-policy">Accumulating</td></tr>
  <tr><td data-testid="tl_etf-basics_value_domicile-country">Ireland</td></tr>
  <tr><td data-testid="tl_etf-basics_value_fund-provider">iShares</td></tr>
</table></html>
"""


def test_ucits_sync_enriches_only_etfs_with_valid_isins(tmp_path):
    store = BarStore(tmp_path)
    fetched_urls: list[str] = []
    metadata = {
        "IWDA": {
            "con_id": 1,
            "provider": "ibkr",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
        },
        "ASML": {
            "con_id": 2,
            "provider": "ibkr",
            "stock_type": "COMMON",
            "isin": "NL0010273215",
        },
        "BROKEN": {
            "con_id": 3,
            "provider": "ibkr",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y984",
        },
        "SPY": {
            "con_id": 4,
            "provider": "ibkr",
            "stock_type": "ETF",
            "isin": "US78462F1030",
        },
    }
    for symbol, fields in metadata.items():
        store.write_instrument_metadata(symbol, fields)
    def fetcher(url: str) -> str:
        fetched_urls.append(url)
        return PROFILE_HTML

    provider = JustEtfProvider(store, fetcher=fetcher)

    result = sync_ucits_profiles(
        store,
        metadata,
        provider,
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )

    assert set(result) == {"IWDA", "BROKEN"}
    assert result["IWDA"].freshness.value == "FRESH"
    assert result["BROKEN"].freshness.value == "MISSING"
    iwda = store.read_instrument_metadata("IWDA")
    assert iwda["provider"] == "ibkr"  # price provenance is untouched
    assert iwda["ucits_profile_isin"] == "IE00B4L5Y983"
    assert iwda["ucits_profile_status"] == "FRESH"
    assert "ucits_profile_status" not in store.read_instrument_metadata("ASML")
    assert "ucits_profile_status" not in store.read_instrument_metadata("SPY")
    assert all("US78462F1030" not in url for url in fetched_urls)


def test_ucits_sync_isolates_one_profile_failure(tmp_path):
    store = BarStore(tmp_path)
    metadata = {
        "IWDA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00B4L5Y983"},
        "EXSA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00BZ17CN18"},
    }
    for symbol, fields in metadata.items():
        store.write_instrument_metadata(symbol, fields)

    def fetch(url: str) -> str:
        if "IE00BZ17CN18" in url:
            raise TimeoutError("provider unavailable")
        return PROFILE_HTML

    result = sync_ucits_profiles(
        store,
        metadata,
        JustEtfProvider(store, fetcher=fetch),
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )

    assert result["IWDA"].freshness.value == "FRESH"
    assert result["EXSA"].freshness.value == "MISSING"
    assert store.read_instrument_metadata("EXSA")["provider"] == "ibkr"
    assert store.read_instrument_metadata("EXSA")["ucits_profile_status"] == "MISSING"


def test_ucits_sync_paces_profile_candidates(tmp_path):
    store = BarStore(tmp_path)
    metadata = {
        "IWDA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00B4L5Y983"},
        "EXSA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00BZ17CN18"},
    }
    waits: list[float] = []

    result = sync_ucits_profiles(
        store,
        metadata,
        JustEtfProvider(store, fetcher=lambda _url: PROFILE_HTML),
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
        pace_seconds=1.25,
        sleeper=waits.append,
    )

    assert set(result) == {"IWDA", "EXSA"}
    assert waits == [1.25]


def test_ucits_sync_publishes_listing_statuses_in_one_batch(tmp_path, monkeypatch):
    store = BarStore(tmp_path)
    metadata = {
        "IWDA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00B4L5Y983"},
        "EXSA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00BZ17CN18"},
    }
    writes: list[dict[str, dict]] = []
    original = store.write_instrument_metadata_batch

    def record(updates: dict[str, dict]) -> None:
        writes.append(updates)
        original(updates)

    monkeypatch.setattr(store, "write_instrument_metadata_batch", record)

    sync_ucits_profiles(
        store,
        metadata,
        JustEtfProvider(store, fetcher=lambda _url: PROFILE_HTML),
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )

    assert len(writes) == 1
    assert set(writes[0]) == {"IWDA", "EXSA"}
