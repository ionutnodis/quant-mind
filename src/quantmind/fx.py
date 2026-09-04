"""Dated, provenance-backed FX normalization for portfolio analytics.

The ECB publishes reference rates as units of currency per EUR.  QuantMind
normalizes those observations to ``USD per currency`` because that is the
canonical quote used by the book contracts: a value in currency ``c`` is
converted to base ``b`` by multiplying by ``q[c] / q[b]``.  One quote per
currency also makes the N-1 independent-currency-factor structure explicit.

ECB rates are informational reference rates, not executable broker marks.
Callers must surface the source/as-of metadata and this module fails closed
when a required observation is missing, non-positive, future-dated, or stale.
"""

from __future__ import annotations

import io
import hashlib
import json
import math
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import certifi

from quantmind.sources.http import read_bounded_text


_ECB_API = "https://data-api.ecb.europa.eu/service/data/EXR"
_MANIFEST = "fx_manifest.json"
_ROLLBACK_MANIFEST = "fx_manifest.rollback.json"
_SCHEMA_VERSION = "ecb_fx_v2"
_SUPPORTED_SCHEMA_VERSIONS = {"ecb_fx_v1", _SCHEMA_VERSION}
_QUOTE_BASIS = "USD_PER_CURRENCY"
_MAX_ECB_RESPONSE_BYTES = 20 * 1024 * 1024


class FxConversionUnavailable(ValueError):
    """A required, trustworthy dated FX observation is not available."""


class FxObservationStale(FxConversionUnavailable):
    """The latest available observation exceeds the permitted carry window."""


def _currency(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError(f"invalid ISO currency code {value!r}")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _series_name(currency: str) -> str:
    return f"FX_USD_PER_{_currency(currency)}"


def _generation_series_name(currency: str, generation: str) -> str:
    return f"{_series_name(currency)}__{generation}"


def _valid_series_name(currency: str, name: object) -> bool:
    """Accept legacy canonical files and immutable generation-addressed files."""
    if not isinstance(name, str):
        return False
    canonical = _series_name(currency)
    if name == canonical:
        return True
    prefix = f"{canonical}__"
    suffix = name.removeprefix(prefix)
    return name.startswith(prefix) and len(suffix) == 12 and all(
        char in "0123456789abcdef" for char in suffix
    )


def _valid_generation(value: object) -> bool:
    return isinstance(value, str) and len(value) == 12 and all(
        character in "0123456789abcdef" for character in value
    )


def _manifest_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise FxConversionUnavailable(
            f"FX provenance manifest has an invalid {field}"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FxConversionUnavailable(
            f"FX provenance manifest has an invalid {field}"
        ) from exc
    if parsed.isoformat() != value:
        raise FxConversionUnavailable(
            f"FX provenance manifest has an invalid {field}"
        )
    return parsed


def parse_ecb_reference_rates(
    csv_text: str,
    currencies: Iterable[str],
) -> dict[str, pd.Series]:
    """Parse ECB CSV and return positive USD-per-currency observations.

    Crosses are joined to the USD/EUR observation on the same date.  Missing
    dates are omitted rather than filled here; controlled backward-looking
    carry-forward happens only when a consumer requests a dated valuation.
    """

    requested = {_currency(item) for item in currencies}
    if not requested:
        return {}
    try:
        frame = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:
        raise FxConversionUnavailable("ECB response is not valid CSV") from exc
    required = {"CURRENCY", "TIME_PERIOD", "OBS_VALUE"}
    if not required.issubset(frame.columns):
        raise FxConversionUnavailable(
            f"ECB response is missing columns: {sorted(required - set(frame.columns))}"
        )

    work = frame.loc[:, ["CURRENCY", "TIME_PERIOD", "OBS_VALUE"]].copy()
    work["CURRENCY"] = work["CURRENCY"].astype(str).str.strip().str.upper()
    work["TIME_PERIOD"] = pd.to_datetime(work["TIME_PERIOD"], errors="coerce")
    work["OBS_VALUE"] = pd.to_numeric(work["OBS_VALUE"], errors="coerce")
    work = work.dropna()
    work = work[work["OBS_VALUE"].map(lambda value: math.isfinite(value) and value > 0)]
    if work.empty:
        raise FxConversionUnavailable("ECB response contains no usable observations")

    pivot = work.pivot_table(
        index="TIME_PERIOD", columns="CURRENCY", values="OBS_VALUE", aggfunc="last"
    ).sort_index()
    if "USD" not in pivot:
        raise FxConversionUnavailable("ECB response contains no USD/EUR reference rate")

    usd_per_eur = pivot["USD"].dropna()
    result: dict[str, pd.Series] = {}
    for currency in sorted(requested):
        if currency == "USD":
            result[currency] = pd.Series(1.0, index=usd_per_eur.index, name="value")
        elif currency == "EUR":
            result[currency] = usd_per_eur.rename("value")
        elif currency in pivot:
            aligned = pd.concat(
                {"usd_per_eur": usd_per_eur, "currency_per_eur": pivot[currency]},
                axis=1,
                join="inner",
            ).dropna()
            result[currency] = (
                aligned["usd_per_eur"] / aligned["currency_per_eur"]
            ).rename("value")
    return result


class EcbFxProvider:
    """Small official-data adapter with an injected fetch seam for tests."""

    name = "ECB"

    def __init__(self, fetcher: Callable[[str], str] | None = None, timeout: float = 20.0):
        self._fetcher = fetcher or self._fetch
        self.timeout = timeout

    def _fetch(self, url: str) -> str:
        request = Request(
            url,
            headers={"Accept": "text/csv", "User-Agent": "QuantMind (+local research)"},
        )
        context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(  # noqa: S310 - fixed HTTPS host
            request, timeout=self.timeout, context=context
        ) as response:
            return read_bounded_text(
                response,
                max_bytes=_MAX_ECB_RESPONSE_BYTES,
                encoding="utf-8",
            )

    def url(self, currencies: Iterable[str], *, start: date, end: date) -> str:
        requested = {_currency(item) for item in currencies}
        # USD/EUR is required to construct every canonical cross. EUR has no
        # ECB currency leg because it is the denominator of the source data.
        source_currencies = sorted((requested | {"USD"}) - {"EUR"})
        key = f"D.{'+'.join(source_currencies)}.EUR.SP00.A"
        query = urlencode(
            {
                "startPeriod": start.isoformat(),
                "endPeriod": end.isoformat(),
                "format": "csvdata",
            }
        )
        return f"{_ECB_API}/{key}?{query}"

    def reference_rates(
        self,
        currencies: Iterable[str],
        *,
        start: date,
        end: date,
    ) -> tuple[dict[str, pd.Series], str]:
        requested = {_currency(item) for item in currencies}
        url = self.url(requested, start=start, end=end)
        return parse_ecb_reference_rates(self._fetcher(url), requested), url


@dataclass(frozen=True)
class FxSyncResult:
    currencies: tuple[str, ...]
    as_of: str
    manifest: dict


def _manifest_path(store) -> Path:
    return Path(store.root) / _MANIFEST


def _rollback_manifest_path(store) -> Path:
    return Path(store.root) / _ROLLBACK_MANIFEST


def read_fx_manifest(store) -> dict:
    path = _manifest_path(store)
    if not path.exists():
        raise FxConversionUnavailable("FX provenance manifest is missing")
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise FxConversionUnavailable("FX provenance manifest is corrupt") from exc
    if not isinstance(payload, dict):
        raise FxConversionUnavailable("FX provenance manifest is unsupported")
    schema_version = payload.get("schema_version")
    series = payload.get("series")
    if (
        schema_version not in _SUPPORTED_SCHEMA_VERSIONS
        or payload.get("quote_basis") != _QUOTE_BASIS
        or payload.get("provider") != "ECB"
        or not isinstance(series, dict)
    ):
        raise FxConversionUnavailable("FX provenance manifest is unsupported")
    if schema_version == _SCHEMA_VERSION:
        generation = payload.get("generation")
        if not _valid_generation(generation) or not series:
            raise FxConversionUnavailable(
                "FX provenance manifest has an invalid generation"
            )
        series_dates: list[date] = []
        for currency, entry in series.items():
            try:
                normalized_currency = _currency(currency)
            except ValueError as exc:
                raise FxConversionUnavailable(
                    "FX provenance manifest has an invalid currency"
                ) from exc
            if normalized_currency != currency or not isinstance(entry, dict):
                raise FxConversionUnavailable(
                    "FX provenance manifest has an invalid series entry"
                )
            if entry.get("name") != _generation_series_name(currency, generation):
                raise FxConversionUnavailable(
                    f"FX provenance manifest has no canonical {currency} series"
                )
            series_dates.append(
                _manifest_date(entry.get("as_of"), field=f"{currency} as-of date")
            )
        manifest_as_of = _manifest_date(payload.get("as_of"), field="as-of date")
        if manifest_as_of != min(series_dates):
            raise FxConversionUnavailable(
                "FX provenance manifest as-of does not match its series"
            )
    return payload


def _read_cached_fx_series(store, currency: str, entry: object) -> pd.Series:
    """Load one manifest-bound FX series and reject malformed cache structure."""

    name = entry.get("name") if isinstance(entry, dict) else None
    if not _valid_series_name(currency, name):
        raise FxConversionUnavailable(
            f"FX provenance manifest has no canonical {currency} series"
        )
    try:
        values = store.read_series(name)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise FxConversionUnavailable(
            f"cached {currency} FX series is unavailable"
        ) from exc
    if (
        not isinstance(values.index, pd.DatetimeIndex)
        or values.index.hasnans
        or not values.index.is_unique
        or not values.index.is_monotonic_increasing
    ):
        raise FxConversionUnavailable(
            f"cached {currency} FX series has an invalid date index"
        )
    index = values.index
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    clean = values.copy()
    clean.index = index
    clean = clean.dropna()
    numeric = pd.to_numeric(clean, errors="coerce")
    numeric = numeric[
        numeric.map(
            lambda value: math.isfinite(float(value)) and float(value) > 0
        )
    ]
    if numeric.empty:
        raise FxConversionUnavailable(f"cached {currency} FX series is empty")
    declared_as_of = _manifest_date(
        entry.get("as_of") if isinstance(entry, dict) else None,
        field=f"{currency} as-of date",
    )
    if numeric.index[-1].date() != declared_as_of:
        raise FxConversionUnavailable(
            f"cached {currency} FX series as-of disagrees with its manifest"
        )
    return numeric.astype(float)


def _write_json_atomically(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _prune_fx_generations(store, retained_names: set[str]) -> None:
    """Best-effort reclamation after the new manifest is already durable."""

    for name in store.list_series():
        if name in retained_names or "__" not in name:
            continue
        canonical, generation = name.rsplit("__", 1)
        currency = canonical.removeprefix("FX_USD_PER_")
        try:
            is_canonical = canonical == _series_name(currency)
        except ValueError:
            is_canonical = False
        if not is_canonical or not _valid_generation(generation):
            continue
        try:
            (Path(store.root) / "series" / f"{name}.parquet").unlink(missing_ok=True)
        except OSError:
            # Cleanup must never invalidate a successfully published generation.
            continue


def sync_ecb_fx(
    store,
    provider: EcbFxProvider,
    currencies: Iterable[str],
    *,
    today: date | None = None,
    years: int = 5,
    fetched_at: str | None = None,
) -> FxSyncResult:
    """Fetch, merge, and atomically publish ECB reference-rate evidence."""

    requested = {_currency(item) for item in currencies}
    if not requested:
        raise ValueError("at least one currency is required")
    end = today or date.today()
    start = end - timedelta(days=max(1, years) * 366)
    rates, source_url = provider.reference_rates(requested, start=start, end=end)
    missing = requested - set(rates)
    if missing:
        raise FxConversionUnavailable(
            f"ECB returned no usable reference rates for {sorted(missing)}"
        )
    end_timestamp = pd.Timestamp(end)
    for currency, values in rates.items():
        if not isinstance(values.index, pd.DatetimeIndex) or values.index.hasnans:
            raise FxConversionUnavailable(
                f"ECB returned an invalid date index for {currency}"
            )
        index = values.index
        if index.tz is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        if (index.normalize() > end_timestamp).any():
            raise FxConversionUnavailable(
                f"ECB returned {currency} observations after sync end {end}"
            )

    publication_time = fetched_at or _utc_now()
    try:
        previous_manifest = read_fx_manifest(store)
    except FxConversionUnavailable:
        previous_manifest = None

    previous_series: dict[str, pd.Series] = {}
    previous_complete = previous_manifest is not None
    if previous_manifest is not None:
        for manifest_currency, entry in previous_manifest["series"].items():
            try:
                currency = _currency(manifest_currency)
                if currency != manifest_currency:
                    raise ValueError("non-canonical currency")
                previous_series[currency] = _read_cached_fx_series(
                    store, currency, entry
                )
            except (FxConversionUnavailable, ValueError):
                previous_complete = False

    merged_series: dict[str, pd.Series] = {}
    latest_dates: list[pd.Timestamp] = []
    published_currencies = requested | set(previous_series)
    for currency in sorted(published_currencies):
        if currency in requested:
            new = rates[currency].dropna().sort_index()
            if new.empty:
                raise FxConversionUnavailable(
                    f"ECB returned no usable reference rates for {currency}"
                )
            existing = previous_series.get(currency)
            if existing is not None:
                keep = existing.loc[~existing.index.isin(new.index)]
                new = pd.concat([keep, new]).sort_index()
            values = new
        else:
            values = previous_series[currency]
        merged_series[currency] = values
        last = pd.Timestamp(values.index[-1])
        latest_dates.append(last)

    # The immutable generation identity includes the actual merged dataset,
    # not just wall-clock metadata. Two refreshes in the same second that
    # return different rates can therefore never overwrite one another.
    generation_digest = hashlib.sha256()
    generation_digest.update(publication_time.encode())
    generation_digest.update(source_url.encode())
    for currency, values in sorted(merged_series.items()):
        generation_digest.update(currency.encode())
        index = pd.DatetimeIndex(pd.to_datetime(values.index))
        if index.tz is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        generation_digest.update(index.asi8.tobytes())
        generation_digest.update(values.to_numpy(dtype="float64").tobytes())
    generation = generation_digest.hexdigest()[:12]

    series_meta: dict[str, dict[str, str]] = {}
    staged_series: dict[str, pd.Series] = {}
    for currency, values in sorted(merged_series.items()):
        name = _generation_series_name(currency, generation)
        staged_series[name] = values
        last = pd.Timestamp(values.index[-1])
        series_meta[currency] = {"name": name, "as_of": last.date().isoformat()}

    # Publish every immutable data file first. If any write fails, the old
    # manifest remains the sole dataset pointer and readers cannot observe a
    # mixed generation. Orphaned generation files are harmless and may be
    # reclaimed only after a later successful publication.
    for name, values in staged_series.items():
        store.write_series(name, values)

    as_of = min(latest_dates).date().isoformat()
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "quote_basis": _QUOTE_BASIS,
        "provider": provider.name,
        "source_url": source_url,
        "fetched_at": publication_time,
        "generation": generation,
        "as_of": as_of,
        "series": series_meta,
    }
    rollback_manifest = previous_manifest if previous_complete else None
    rollback_path = _rollback_manifest_path(store)
    if rollback_manifest is not None:
        _write_json_atomically(rollback_path, rollback_manifest)
    _write_json_atomically(_manifest_path(store), manifest)

    retained_names = {entry["name"] for entry in manifest["series"].values()}
    if rollback_manifest is not None:
        retained_names.update(
            entry["name"]
            for entry in rollback_manifest["series"].values()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        )
    else:
        try:
            rollback_path.unlink(missing_ok=True)
        except OSError:
            pass
    _prune_fx_generations(store, retained_names)
    return FxSyncResult(
        currencies=tuple(sorted(merged_series)),
        as_of=as_of,
        manifest=manifest,
    )


@dataclass(frozen=True)
class FxConverter:
    """Dated currency converter built from canonical USD-per-currency series."""

    base_currency: str
    usd_per_currency: dict[str, pd.Series]
    source: str
    source_url: str
    fetched_at: str
    max_age_days: int = 7

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_currency", _currency(self.base_currency))
        if self.max_age_days < 0:
            raise ValueError("max_age_days must be non-negative")

    @property
    def as_of(self) -> str | None:
        relevant = [
            pd.Timestamp(series.dropna().index[-1])
            for currency, series in self.usd_per_currency.items()
            if currency != "USD" and not series.dropna().empty
        ]
        return min(relevant).date().isoformat() if relevant else None

    @classmethod
    def from_store(
        cls,
        store,
        *,
        base_currency: str,
        currencies: Iterable[str],
        max_age_days: int = 7,
    ) -> "FxConverter":
        base = _currency(base_currency)
        requested = {_currency(item) for item in currencies} | {base}
        if requested == {base}:
            return cls(
                base_currency=base,
                usd_per_currency={},
                source="identity",
                source_url="",
                fetched_at="",
                max_age_days=max_age_days,
            )

        manifest = read_fx_manifest(store)
        series_manifest = manifest["series"]
        to_load = (requested | {"USD"}) if base != "USD" else requested
        series: dict[str, pd.Series] = {}
        for currency in sorted(to_load):
            if currency == "USD" and currency not in series_manifest:
                # Older-but-valid manifests may omit the mathematical identity.
                continue
            entry = series_manifest.get(currency)
            series[currency] = _read_cached_fx_series(store, currency, entry)

        return cls(
            base_currency=base,
            usd_per_currency=series,
            source=str(manifest["provider"]),
            source_url=str(manifest.get("source_url") or ""),
            fetched_at=str(manifest.get("fetched_at") or ""),
            max_age_days=max_age_days,
        )

    def _quote(self, currency: str, when: pd.Timestamp) -> float:
        normalized = _currency(currency)
        if normalized == "USD":
            return 1.0
        series = self.usd_per_currency.get(normalized)
        if series is None or series.empty:
            raise FxConversionUnavailable(f"no dated {normalized} FX observation")
        candidates = series.loc[series.index <= when]
        if candidates.empty:
            raise FxConversionUnavailable(f"no dated {normalized} FX observation on or before {when.date()}")
        observation_date = pd.Timestamp(candidates.index[-1])
        age = (when.normalize() - observation_date.normalize()).days
        if age > self.max_age_days:
            raise FxObservationStale(
                f"stale {normalized} FX observation from {observation_date.date()}"
            )
        return float(candidates.iloc[-1])

    def rate(self, currency: str, as_of: str | date | pd.Timestamp) -> float:
        source = _currency(currency)
        if source == self.base_currency:
            return 1.0
        when = pd.Timestamp(as_of)
        if when.tzinfo is not None:
            when = when.tz_convert("UTC").tz_localize(None)
        source_quote = self._quote(source, when)
        base_quote = self._quote(self.base_currency, when)
        return source_quote / base_quote

    def convert(
        self,
        value: float,
        currency: str,
        as_of: str | date | pd.Timestamp,
    ) -> float:
        return float(value) * self.rate(currency, as_of)

    def convert_series(self, local_prices: pd.Series, currency: str) -> pd.Series:
        source = _currency(currency)
        if source == self.base_currency:
            return local_prices.astype(float).copy()
        index = pd.DatetimeIndex(pd.to_datetime(local_prices.index))
        if index.tz is not None:
            index = index.tz_convert("UTC").tz_localize(None)

        def aligned_quote(target: str) -> pd.Series:
            if target == "USD":
                return pd.Series(1.0, index=index)
            series = self.usd_per_currency.get(target)
            if series is None or series.empty:
                raise FxConversionUnavailable(f"no dated {target} FX observation")
            source_index = pd.DatetimeIndex(pd.to_datetime(series.index))
            if source_index.tz is not None:
                source_index = source_index.tz_convert("UTC").tz_localize(None)
            normalized = pd.Series(series.to_numpy(dtype=float), index=source_index).sort_index()
            aligned = normalized.reindex(
                index,
                method="ffill",
                tolerance=pd.Timedelta(days=self.max_age_days),
            )
            if aligned.isna().any():
                missing_date = index[aligned.isna()][0]
                raise FxConversionUnavailable(
                    f"no dated {target} FX observation for {missing_date.date()}"
                )
            return aligned

        source_quote = aligned_quote(source)
        base_quote = aligned_quote(self.base_currency)
        return pd.Series(
            local_prices.to_numpy(dtype=float) * source_quote.to_numpy() / base_quote.to_numpy(),
            index=local_prices.index,
            name=local_prices.name,
        )
