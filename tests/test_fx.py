"""Dated FX normalization goldens for European portfolios.

ECB observations are quoted as units of currency per EUR.  QuantMind stores
one canonical quote instead: USD per one unit of each currency.  That makes
triangulation explicit and leaves only N-1 independent FX factors.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from quantmind.datastore.store import BarStore
from quantmind.fx import (
    EcbFxProvider,
    FxConversionUnavailable,
    FxConverter,
    FxObservationStale,
    parse_ecb_reference_rates,
    read_fx_manifest,
    sync_ecb_fx,
)


ECB_CSV = """KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE
EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-01,1.1000
EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-09-01,0.8800
EXR.D.CHF.EUR.SP00.A,D,CHF,EUR,SP00,A,2026-09-01,0.9350
EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-02,1.1200
EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-09-02,0.8750
EXR.D.CHF.EUR.SP00.A,D,CHF,EUR,SP00,A,2026-09-02,0.9400
"""


def test_parse_ecb_rates_builds_canonical_usd_per_currency_series():
    rates = parse_ecb_reference_rates(ECB_CSV, {"USD", "EUR", "GBP", "CHF"})

    assert list(rates) == ["CHF", "EUR", "GBP", "USD"]
    assert rates["USD"].iloc[-1] == 1.0
    assert rates["EUR"].iloc[-1] == pytest.approx(1.12)
    assert rates["GBP"].iloc[-1] == pytest.approx(1.12 / 0.875)
    assert rates["CHF"].iloc[-1] == pytest.approx(1.12 / 0.94)
    assert rates["GBP"].index[-1] == pd.Timestamp("2026-09-02")


def test_parse_ecb_rates_fails_closed_when_usd_cross_is_missing():
    csv = "\n".join(
        line for line in ECB_CSV.splitlines() if ",USD,EUR," not in line
    )

    with pytest.raises(FxConversionUnavailable, match="no USD/EUR"):
        parse_ecb_reference_rates(csv, {"GBP"})


def test_parse_ecb_rates_uses_only_dates_with_both_cross_legs():
    csv = ECB_CSV.replace(
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-02,1.1200\n", ""
    )

    rates = parse_ecb_reference_rates(csv, {"GBP"})

    assert rates["GBP"].index.tolist() == [pd.Timestamp("2026-09-01")]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not,csv", "missing columns"),
        ("CURRENCY,TIME_PERIOD,OBS_VALUE\nUSD,bad,-1\n", "no usable"),
    ],
)
def test_parse_ecb_rates_rejects_malformed_or_unusable_payloads(payload, message):
    with pytest.raises(FxConversionUnavailable, match=message):
        parse_ecb_reference_rates(payload, {"EUR"})


def test_failed_ecb_sync_with_missing_requested_cross_publishes_nothing(tmp_path):
    store = BarStore(tmp_path)
    csv = "CURRENCY,TIME_PERIOD,OBS_VALUE\nUSD,2026-09-02,1.12\n"

    with pytest.raises(FxConversionUnavailable, match="GBP"):
        sync_ecb_fx(
            store,
            EcbFxProvider(fetcher=lambda _url: csv),
            {"USD", "GBP"},
            today=date(2026, 9, 2),
        )

    assert not (tmp_path / "fx_manifest.json").exists()
    assert store.list_series() == []


def test_failed_ecb_sync_with_disjoint_cross_dates_publishes_nothing(tmp_path):
    store = BarStore(tmp_path)
    csv = """CURRENCY,TIME_PERIOD,OBS_VALUE
USD,2026-09-01,1.12
GBP,2026-09-02,0.87
"""

    with pytest.raises(FxConversionUnavailable, match="no usable.*GBP"):
        sync_ecb_fx(
            store,
            EcbFxProvider(fetcher=lambda _url: csv),
            {"USD", "GBP"},
            today=date(2026, 9, 2),
        )

    assert not (tmp_path / "fx_manifest.json").exists()
    assert store.list_series() == []


def test_ecb_sync_rejects_observations_after_the_requested_end(tmp_path):
    future_csv = ECB_CSV + (
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-03,1.1300\n"
    )
    store = BarStore(tmp_path)

    with pytest.raises(FxConversionUnavailable, match="after sync end"):
        sync_ecb_fx(
            store,
            EcbFxProvider(fetcher=lambda _url: future_csv),
            {"USD", "EUR"},
            today=date(2026, 9, 2),
        )

    assert not (tmp_path / "fx_manifest.json").exists()
    assert store.list_series() == []


def test_converter_triangulates_spot_and_date_aligned_price_history():
    idx = pd.to_datetime(["2026-09-01", "2026-09-02"])
    converter = FxConverter(
        base_currency="GBP",
        usd_per_currency={
            "USD": pd.Series([1.0, 1.0], index=idx),
            "EUR": pd.Series([1.10, 1.12], index=idx),
            "GBP": pd.Series([1.25, 1.28], index=idx),
        },
        source="ECB",
        source_url="https://data-api.ecb.europa.eu/",
        fetched_at="2026-09-02T17:00:00Z",
    )

    assert converter.rate("EUR", "2026-09-02") == pytest.approx(1.12 / 1.28)
    assert converter.convert(100.0, "EUR", "2026-09-02") == pytest.approx(87.5)
    local = pd.Series([100.0, 110.0], index=idx)
    converted = converter.convert_series(local, "EUR")
    assert converted.iloc[0] == pytest.approx(88.0)
    assert converted.iloc[1] == pytest.approx(96.25)


def test_converter_does_not_use_future_or_too_old_fx_observation():
    converter = FxConverter(
        base_currency="USD",
        usd_per_currency={
            "USD": pd.Series([1.0], index=pd.to_datetime(["2026-09-01"])),
            "EUR": pd.Series([1.10], index=pd.to_datetime(["2026-09-01"])),
        },
        source="ECB",
        source_url="https://data-api.ecb.europa.eu/",
        fetched_at="2026-09-01T17:00:00Z",
        max_age_days=7,
    )

    with pytest.raises(FxConversionUnavailable, match="no dated EUR"):
        converter.rate("EUR", "2026-08-31")
    with pytest.raises(FxObservationStale, match="stale EUR"):
        converter.rate("EUR", "2026-09-10")


def test_converter_as_of_reports_the_observation_used_not_the_cache_watermark():
    converter = FxConverter(
        base_currency="USD",
        usd_per_currency={
            "EUR": pd.Series(
                [1.10, 1.20],
                index=pd.to_datetime(["2026-07-24", "2026-09-04"]),
            ),
        },
        source="ECB",
        source_url="https://data-api.ecb.europa.eu/",
        fetched_at="2026-09-04T17:00:00Z",
    )

    assert converter.as_of == "2026-09-04"

    converted = converter.convert_series(
        pd.Series([100.0], index=pd.to_datetime(["2026-07-24"])), "EUR"
    )

    assert converted.iloc[0] == pytest.approx(110.0)
    assert converter.as_of == "2026-07-24"
    assert converter.cache_as_of == "2026-09-04"
    assert converter.fetched_at == "2026-09-04T17:00:00Z"


def test_convert_series_carries_weekends_but_rejects_future_and_stale_quotes():
    converter = FxConverter(
        base_currency="USD",
        usd_per_currency={
            "EUR": pd.Series(
                [1.10, 1.12],
                index=pd.to_datetime(["2026-09-04", "2026-09-14"]),
            ),
        },
        source="ECB",
        source_url="https://data-api.ecb.europa.eu/",
        fetched_at="2026-09-14T17:00:00Z",
        max_age_days=7,
    )

    weekend = converter.convert_series(
        pd.Series([100.0], index=pd.to_datetime(["2026-09-07"])), "EUR"
    )
    assert weekend.iloc[0] == pytest.approx(110.0)

    with pytest.raises(FxConversionUnavailable, match="2026-09-03"):
        converter.convert_series(
            pd.Series([100.0], index=pd.to_datetime(["2026-09-03"])), "EUR"
        )
    with pytest.raises(FxConversionUnavailable, match="2026-09-12"):
        converter.convert_series(
            pd.Series([100.0], index=pd.to_datetime(["2026-09-12"])), "EUR"
        )


def test_convert_series_triangulates_every_date_for_a_non_usd_base():
    index = pd.to_datetime(["2026-09-04", "2026-09-07"])
    converter = FxConverter(
        base_currency="GBP",
        usd_per_currency={
            "EUR": pd.Series([1.10], index=pd.to_datetime(["2026-09-04"])),
            "GBP": pd.Series([1.25], index=pd.to_datetime(["2026-09-04"])),
        },
        source="ECB",
        source_url="https://data-api.ecb.europa.eu/",
        fetched_at="2026-09-07T17:00:00Z",
    )

    converted = converter.convert_series(pd.Series([100.0, 110.0], index=index), "EUR")

    assert converted.tolist() == pytest.approx([88.0, 96.8])


def test_ecb_sync_persists_series_and_provenance_manifest(tmp_path):
    store = BarStore(tmp_path)
    provider = EcbFxProvider(fetcher=lambda _url: ECB_CSV)

    result = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR", "GBP"},
        today=date(2026, 9, 2),
        years=1,
        fetched_at="2026-09-02T17:00:00Z",
    )

    assert result.currencies == ("EUR", "GBP", "USD")
    assert result.as_of == "2026-09-02"
    manifest = result.manifest
    assert manifest["schema_version"] == "ecb_fx_v2"
    assert manifest["quote_basis"] == "USD_PER_CURRENCY"
    assert manifest["provider"] == "ECB"
    assert manifest["series"]["GBP"]["as_of"] == "2026-09-02"
    assert store.read_series(manifest["series"]["GBP"]["name"]).iloc[-1] == pytest.approx(
        1.28
    )


def test_ecb_subset_refresh_preserves_previously_published_currencies(tmp_path):
    store = BarStore(tmp_path)
    provider = EcbFxProvider(fetcher=lambda _url: ECB_CSV)
    original = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR"},
        today=date(2026, 9, 2),
        years=1,
        fetched_at="2026-09-02T17:00:00Z",
    )

    refreshed = sync_ecb_fx(
        store,
        provider,
        {"USD", "GBP"},
        today=date(2026, 9, 2),
        years=1,
        fetched_at="2026-09-02T18:00:00Z",
    )

    assert refreshed.currencies == ("EUR", "GBP", "USD")
    assert set(refreshed.manifest["series"]) == {"EUR", "GBP", "USD"}
    assert (
        refreshed.manifest["series"]["EUR"]["name"]
        != original.manifest["series"]["EUR"]["name"]
    )
    converter = FxConverter.from_store(
        store, base_currency="USD", currencies={"EUR", "GBP"}
    )
    assert converter.rate("EUR", "2026-09-02") == pytest.approx(1.12)
    assert converter.rate("GBP", "2026-09-02") == pytest.approx(1.28)


def test_ecb_sync_retains_only_active_and_one_rollback_generation(tmp_path):
    store = BarStore(tmp_path)
    provider = EcbFxProvider(fetcher=lambda _url: ECB_CSV)
    first = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR"},
        today=date(2026, 9, 2),
        fetched_at="2026-09-02T17:00:00Z",
    )
    second = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR"},
        today=date(2026, 9, 2),
        fetched_at="2026-09-02T18:00:00Z",
    )
    third = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR"},
        today=date(2026, 9, 2),
        fetched_at="2026-09-02T19:00:00Z",
    )

    rollback = json.loads((tmp_path / "fx_manifest.rollback.json").read_text())
    assert rollback == second.manifest
    assert read_fx_manifest(store) == third.manifest
    retained_generations = {
        name.rsplit("__", 1)[-1]
        for name in store.list_series()
        if name.startswith("FX_USD_PER_") and "__" in name
    }
    assert retained_generations == {
        second.manifest["generation"],
        third.manifest["generation"],
    }
    assert first.manifest["generation"] not in retained_generations
    for entry in rollback["series"].values():
        assert not store.read_series(entry["name"]).empty


def test_ecb_sync_failure_cannot_publish_a_mixed_generation(tmp_path, monkeypatch):
    store = BarStore(tmp_path)
    provider = EcbFxProvider(fetcher=lambda _url: ECB_CSV)
    original = sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR", "GBP"},
        today=date(2026, 9, 2),
        years=1,
        fetched_at="2026-09-02T17:00:00Z",
    )
    original_write = store.write_series
    writes = 0

    def fail_during_generation(name, values):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated interrupted refresh")
        original_write(name, values)

    monkeypatch.setattr(store, "write_series", fail_during_generation)
    changed_csv = ECB_CSV.replace("1.1200", "1.2200").replace("0.8750", "0.7750")
    with pytest.raises(OSError, match="interrupted"):
        sync_ecb_fx(
            store,
            EcbFxProvider(fetcher=lambda _url: changed_csv),
            {"USD", "EUR", "GBP"},
            today=date(2026, 9, 2),
            years=1,
            fetched_at="2026-09-02T17:00:00Z",
        )

    assert read_fx_manifest(store) == original.manifest
    converter = FxConverter.from_store(
        store, base_currency="USD", currencies={"EUR", "GBP"}
    )
    assert converter.rate("EUR", "2026-09-02") == pytest.approx(1.12)
    assert converter.rate("GBP", "2026-09-02") == pytest.approx(1.28)


def test_converter_loads_only_manifest_backed_requested_currencies(tmp_path):
    store = BarStore(tmp_path)
    provider = EcbFxProvider(fetcher=lambda _url: ECB_CSV)
    sync_ecb_fx(
        store,
        provider,
        {"USD", "EUR", "GBP"},
        today=date(2026, 9, 2),
        years=1,
        fetched_at="2026-09-02T17:00:00Z",
    )

    converter = FxConverter.from_store(
        store,
        base_currency="EUR",
        currencies={"GBP"},
        max_age_days=7,
    )

    assert converter.rate("GBP", "2026-09-02") == pytest.approx(1.28 / 1.12)
    assert converter.as_of == "2026-09-02"


def test_converter_load_fails_closed_without_manifest(tmp_path):
    store = BarStore(tmp_path)
    store.write_series(
        "FX_USD_PER_EUR",
        pd.Series([1.1], index=pd.to_datetime(["2026-09-02"])),
    )

    with pytest.raises(FxConversionUnavailable, match="manifest"):
        FxConverter.from_store(store, base_currency="USD", currencies={"EUR"})


def test_converter_loads_a_legacy_v1_canonical_manifest(tmp_path):
    store = BarStore(tmp_path)
    store.write_series(
        "FX_USD_PER_EUR",
        pd.Series([1.1], index=pd.to_datetime(["2026-09-02"])),
    )
    (tmp_path / "fx_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ecb_fx_v1",
                "quote_basis": "USD_PER_CURRENCY",
                "provider": "ECB",
                "source_url": "https://data-api.ecb.europa.eu/",
                "fetched_at": "2026-09-02T17:00:00Z",
                "as_of": "2026-09-02",
                "series": {
                    "EUR": {"name": "FX_USD_PER_EUR", "as_of": "2026-09-02"}
                },
            }
        )
    )

    converter = FxConverter.from_store(
        store, base_currency="USD", currencies={"EUR"}
    )

    assert converter.rate("EUR", "2026-09-02") == pytest.approx(1.1)


@pytest.mark.parametrize(
    ("generation", "series_name", "manifest_as_of", "series_as_of"),
    [
        (None, "FX_USD_PER_EUR__0123456789ab", "2026-09-02", "2026-09-02"),
        (
            "0123456789ab",
            "FX_USD_PER_EUR__ffffffffffff",
            "2026-09-02",
            "2026-09-02",
        ),
        (
            "0123456789ab",
            "FX_USD_PER_EUR__0123456789ab",
            "2026-09-02",
            "09/02/2026",
        ),
        (
            "0123456789ab",
            "FX_USD_PER_EUR__0123456789ab",
            "2026-09-01",
            "2026-09-02",
        ),
    ],
)
def test_v2_manifest_rejects_incoherent_generation_names_and_as_of_dates(
    tmp_path, generation, series_name, manifest_as_of, series_as_of
):
    store = BarStore(tmp_path)
    payload = {
        "schema_version": "ecb_fx_v2",
        "quote_basis": "USD_PER_CURRENCY",
        "provider": "ECB",
        "generation": generation,
        "as_of": manifest_as_of,
        "series": {"EUR": {"name": series_name, "as_of": series_as_of}},
    }
    (tmp_path / "fx_manifest.json").write_text(json.dumps(payload))

    with pytest.raises(FxConversionUnavailable, match="manifest"):
        read_fx_manifest(store)


def test_converter_rejects_a_series_whose_watermark_disagrees_with_manifest(tmp_path):
    store = BarStore(tmp_path)
    name = "FX_USD_PER_EUR__0123456789ab"
    store.write_series(
        name,
        pd.Series([1.1, 1.12], index=pd.to_datetime(["2026-09-01", "2026-09-02"])),
    )
    (tmp_path / "fx_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ecb_fx_v2",
                "quote_basis": "USD_PER_CURRENCY",
                "provider": "ECB",
                "generation": "0123456789ab",
                "as_of": "2026-09-01",
                "series": {"EUR": {"name": name, "as_of": "2026-09-01"}},
            }
        )
    )

    with pytest.raises(FxConversionUnavailable, match="as-of"):
        FxConverter.from_store(store, base_currency="USD", currencies={"EUR"})


@pytest.mark.parametrize(
    "index",
    [
        pd.RangeIndex(2),
        pd.to_datetime(["2026-09-02", "2026-09-02"]),
    ],
    ids=["non-datetime", "duplicate-dates"],
)
def test_converter_rejects_corrupt_cached_fx_indexes(tmp_path, index):
    store = BarStore(tmp_path)
    name = "FX_USD_PER_EUR__0123456789ab"
    store.write_series(name, pd.Series([1.1, 1.12], index=index))
    (tmp_path / "fx_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ecb_fx_v2",
                "quote_basis": "USD_PER_CURRENCY",
                "provider": "ECB",
                "generation": "0123456789ab",
                "as_of": "2026-09-02",
                "series": {"EUR": {"name": name, "as_of": "2026-09-02"}},
            }
        )
    )

    with pytest.raises(FxConversionUnavailable, match="index"):
        FxConverter.from_store(store, base_currency="USD", currencies={"EUR"})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "future_v99", "unsupported"),
        ("provider", "OTHER", "unsupported"),
        ("quote_basis", "EUR_PER_CURRENCY", "unsupported"),
    ],
)
def test_converter_rejects_unsupported_manifest_contracts(
    tmp_path, field, value, message
):
    store = BarStore(tmp_path)
    (tmp_path / "fx_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ecb_fx_v2",
                "quote_basis": "USD_PER_CURRENCY",
                "provider": "ECB",
                "series": {},
                field: value,
            }
        )
    )

    with pytest.raises(FxConversionUnavailable, match=message):
        FxConverter.from_store(store, base_currency="USD", currencies={"EUR"})


def test_converter_rejects_an_invalid_manifest_series_reference(tmp_path):
    store = BarStore(tmp_path)
    (tmp_path / "fx_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ecb_fx_v2",
                "quote_basis": "USD_PER_CURRENCY",
                "provider": "ECB",
                "generation": "0123456789ab",
                "as_of": "2026-09-02",
                "series": {
                    "EUR": {"name": "../../outside", "as_of": "2026-09-02"}
                },
            }
        )
    )

    with pytest.raises(FxConversionUnavailable, match="canonical EUR"):
        FxConverter.from_store(store, base_currency="USD", currencies={"EUR"})
