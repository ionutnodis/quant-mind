"""One-shot cache sync: `uv run python -m quantmind.sync_cli [SYMBOLS...]`

Connects to IB Gateway, syncs adjusted daily bars for the universe into the
parquet store, and exits. This process is the designated datastore writer
while it runs (Engineering Constraint 4).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from ib_async import IB

from quantmind.broker.connection import ConnectionManager
from quantmind.broker.ib_broker import IbBroker
from quantmind.config import Settings
from quantmind.datastore.locking import exclusive_sync_lock
from quantmind.datastore.options_store import OptionsStore
from quantmind.datastore.store import BarStore, PORTFOLIO_DISCOVERY_FAILURE_SYMBOL
from quantmind.fx import EcbFxProvider, FxConversionUnavailable, sync_ecb_fx
from quantmind.portfolio import Portfolio
from quantmind.sources.options_sync import sync_options_chains
from quantmind.sources.providers.justetf import JustEtfProvider
from quantmind.sources.providers.yfinance_provider import YFinanceProvider
from quantmind.sources.sync import (
    sync_daily_bars,
    sync_index_bars,
    sync_instrument_metadata,
    sync_yfinance_bars,
)
from quantmind.sources.ucits_sync import sync_ucits_profiles

# World-ETF region tags (Task A2: "wider world") — region metadata cached at
# sync alongside contract details; SH is a negative-beta validation
# instrument, tagged by book rather than geography.
WORLD_ETF_REGIONS = {
    "EZU": "Eurozone",
    "EWU": "United Kingdom",
    "EWY": "South Korea",
    "EWT": "Taiwan",
    "INDA": "India",
    "MCHI": "China",
    "EWZ": "Brazil",
    "EEM": "Emerging Markets",
    "EFA": "Developed ex-US",
    "SH": "US (inverse — negative-beta validation)",
}

# v1 macro-tile universe (design: hedge candidate list is config-editable; this
# is the starter set — crude via USO and DXY via UUP are the constraint-16 proxies)
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "USO", "UUP", "EWJ", "FXI", "EWG",
    # sectors
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    # factor proxies
    "MTUM", "VLUE", "QUAL", "USMV",
    # wider world (Task A2)
    *WORLD_ETF_REGIONS,
]

# VIX + SPX via IBKR Index contracts (empirically verified working: Task A2).
INDEX_UNIVERSE = {"VIX": "CBOE", "SPX": "CBOE"}


def portfolio_sync_targets(portfolio: Portfolio) -> tuple[list[str], list[str]]:
    """Return unique daily-bar targets and held option underliers in book order."""
    daily: list[str] = []
    options: list[str] = []
    for position in portfolio.positions:
        if position.sec_type not in {"STK", "OPT"}:
            continue
        if position.symbol not in daily:
            daily.append(position.symbol)
        if position.sec_type == "OPT" and position.symbol not in options:
            options.append(position.symbol)
    return daily, options


async def _run_main(symbols: list[str], ib_connections: list) -> None:
    settings = Settings()
    store = BarStore(settings.data_dir)
    ib = IB()
    ib_connections.append(ib)
    mgr = ConnectionManager(
        ib, host=settings.host, port=settings.port, client_id=settings.client_id, max_attempts=3
    )
    await mgr.ensure_connected()
    broker = IbBroker(ib, account_id=settings.account_id)
    held_symbols: list[str] = []
    option_underliers: list[str] = []
    held_option_contracts = []
    held_stock_con_ids: dict[str, int | list[int]] = {}
    held_option_con_ids: dict[str, list[int]] = {}
    held_currencies: set[str] = set()
    account_currencies: set[str] = set()
    warnings: list[str] = []
    portfolio_discovery_failed = False
    try:
        portfolio = await broker.get_portfolio()
        held_symbols, option_underliers = portfolio_sync_targets(portfolio)
        held_option_contracts = [
            position for position in portfolio.positions if position.sec_type == "OPT"
        ]
        stock_ids_by_symbol: dict[str, list[int]] = {}
        for position in portfolio.positions:
            if position.sec_type != "STK":
                continue
            stock_ids = stock_ids_by_symbol.setdefault(position.symbol, [])
            if position.con_id not in stock_ids:
                stock_ids.append(position.con_id)
        held_stock_con_ids = {
            symbol: stock_ids[0] if len(stock_ids) == 1 else stock_ids
            for symbol, stock_ids in stock_ids_by_symbol.items()
        }
        for position in held_option_contracts:
            option_ids = held_option_con_ids.setdefault(position.symbol, [])
            if position.con_id not in option_ids:
                option_ids.append(position.con_id)
        held_currencies = {
            position.currency.strip().upper()
            for position in portfolio.positions
            if position.currency and position.currency.strip()
        }
    except Exception as exc:
        portfolio_discovery_failed = True
        warnings.append("live portfolio unavailable")
        print(f"WARNING: live portfolio unavailable ({type(exc).__name__}); syncing starter universe only")

    get_account_summary = getattr(broker, "get_account_summary", None)
    if get_account_summary is not None:
        try:
            account_summary = await get_account_summary()
            account_currency = str(account_summary.get("currency") or "").strip().upper()
            if len(account_currency) == 3 and account_currency.isalpha():
                account_currencies.add(account_currency)
        except Exception as exc:
            print(
                f"WARNING: account-summary currency unavailable ({type(exc).__name__}); "
                "position FX discovery continues"
            )

    sync_symbols = list(
        dict.fromkeys([getattr(settings, "benchmark", "SPY"), *symbols, *held_symbols])
    )
    daily_failures: dict[str, str] = {}
    symbol_map = await sync_daily_bars(
        store,
        broker,
        sync_symbols,
        years=5,
        pace_seconds=2.0,
        known_con_ids=held_stock_con_ids,
        option_contract_con_ids=held_option_con_ids,
        failures=daily_failures,
    )
    if daily_failures:
        warnings.append("daily bars incomplete")
        print(f"WARNING: daily bars unavailable for {daily_failures}")
    for symbol, con_id in symbol_map.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>5} conId={con_id:<12} bars through {wm.date()}")

    index_failures: dict[str, str] = {}
    index_map = await sync_index_bars(
        store,
        broker,
        INDEX_UNIVERSE,
        years=5,
        pace_seconds=2.0,
        failures=index_failures,
    )
    if index_failures:
        warnings.append("index bars incomplete")
        print(f"WARNING: index bars unavailable for {index_failures}")
    for symbol, con_id in index_map.items():
        wm = store.watermark(con_id=con_id, bar_size="1d")
        print(f"{symbol:>5} conId={con_id:<12} bars through {wm.date()} (index)")

    extra_tags = {sym: {"region": region} for sym, region in WORLD_ETF_REGIONS.items()}
    ibkr_map = {**symbol_map, **index_map}
    metadata_failures: dict[str, str] = {}
    instrument_metadata = await sync_instrument_metadata(
        store,
        broker,
        ibkr_map,
        extra_tags=extra_tags,
        pace_seconds=1.0,
        failures=metadata_failures,
    ) or {}
    if metadata_failures:
        warnings.append("instrument metadata incomplete")
        print(f"WARNING: instrument metadata unavailable for {metadata_failures}")
    metadata_currencies = {
        str(fields["currency"]).strip().upper()
        for fields in instrument_metadata.values()
        if fields.get("currency") and str(fields["currency"]).strip()
    }
    if option_underliers:
        try:
            counts = await sync_options_chains(
                OptionsStore(settings.data_dir),
                store,
                ib,
                underliers=option_underliers,
                held_contracts=held_option_contracts,
                pace_seconds=1.0,
            )
            for underlier, count in counts.items():
                print(f"{underlier:>5} snapshotted {count} option contracts")
        except Exception as exc:
            warnings.append("held-option chain unavailable")
            print(
                f"WARNING: held-option chain sync unavailable ({type(exc).__name__}: {exc}); "
                "daily bars remain usable"
            )
    ib.disconnect()

    # justETF is synchronous and independently paced. Run it only after all
    # IBKR work is complete and off the asyncio event loop so a slow profile
    # page cannot starve broker heartbeats or held-option synchronization.
    if getattr(settings, "ucits_metadata_enabled", False):
        try:
            ucits_results = await asyncio.to_thread(
                sync_ucits_profiles,
                store,
                instrument_metadata,
                JustEtfProvider(store),
                now=datetime.now(UTC),
                pace_seconds=1.0,
            )
            incomplete_profiles = sorted(
                symbol
                for symbol, status in ucits_results.items()
                if status.freshness.value != "FRESH"
            )
        except Exception as exc:
            warnings.append("UCITS profiles incomplete")
            print(
                f"WARNING: UCITS profile sync unavailable ({type(exc).__name__}: {exc}); "
                "price sync and later cache phases remain usable"
            )
        else:
            if incomplete_profiles:
                warnings.append("UCITS profiles incomplete")
                print(
                    "WARNING: UCITS profiles unavailable or stale for "
                    f"{incomplete_profiles}; price sync remains usable"
                )
            elif ucits_results:
                print(f"UCITS profiles refreshed for {', '.join(sorted(ucits_results))}")

    yfinance_symbols = settings.yfinance_symbol_list()
    skipped: list[str] = []
    if yfinance_symbols:
        yfinance_failures: dict[str, str] = {}
        yf_map, skipped = sync_yfinance_bars(
            store,
            YFinanceProvider(),
            yfinance_symbols,
            years=5,
            failures=yfinance_failures,
        )
        if yfinance_failures:
            warnings.append("yfinance fallback incomplete")
            print(f"WARNING: yfinance fallback unavailable for {yfinance_failures}")
        for symbol in skipped:
            print(
                f"WARNING: {symbol} is IBKR-synced (positive conId) — skipped yfinance sync; "
                f"remove it from QM_YFINANCE_SYMBOLS (single-provenance law: IBKR wins)"
            )
        for symbol, con_id in yf_map.items():
            if symbol not in skipped:
                print(f"{symbol:>5} conId={con_id:<12} synced via yfinance")

    fallback_currencies = {
        str((store.read_instrument_metadata(symbol) or {}).get("currency") or "")
        .strip()
        .upper()
        for symbol in yfinance_symbols
    } - {""}
    base_currency = getattr(settings, "base_currency", "USD").strip().upper()
    fx_currencies = (
        held_currencies
        | account_currencies
        | metadata_currencies
        | fallback_currencies
        | {base_currency}
    )
    if fx_currencies - {base_currency}:
        provider = EcbFxProvider()
        try:
            fx_result = sync_ecb_fx(store, provider, fx_currencies)
            print(
                f"ECB FX {', '.join(sorted(fx_currencies))} through {fx_result.as_of} "
                "(reference rates)"
            )
        except FxConversionUnavailable as exc:
            # One unsupported/missing ledger currency must not suppress usable
            # ECB evidence for every other European holding. Each successful
            # atomic publication merges the previous manifest, so retries are
            # safe and preserve already cached currencies.
            synced: list[str] = []
            unresolved: list[str] = []
            last_result = None
            for currency in sorted(fx_currencies - {base_currency}):
                try:
                    last_result = sync_ecb_fx(
                        store, provider, {base_currency, currency}
                    )
                    synced.append(currency)
                except Exception:
                    unresolved.append(currency)
            if synced and last_result is not None:
                print(
                    f"ECB FX {base_currency}, {', '.join(synced)} through "
                    f"{last_result.as_of} (reference rates; partial universe)"
                )
                if unresolved:
                    warnings.append("FX reference rates incomplete")
                    print(
                        "WARNING: FX reference rates remain unavailable for "
                        f"{', '.join(unresolved)} ({type(exc).__name__}: {exc}); "
                        "analysis requiring those currencies remains unavailable"
                    )
            else:
                warnings.append("FX reference rates unavailable")
                print(
                    f"WARNING: FX sync unavailable ({type(exc).__name__}: {exc}); "
                    "local-currency bars remain cached but mixed-currency analysis is unavailable"
                )
        except Exception as exc:
            warnings.append("FX reference rates unavailable")
            print(
                f"WARNING: FX sync unavailable ({type(exc).__name__}: {exc}); "
                "local-currency bars remain cached but mixed-currency analysis is unavailable"
            )

    # Readiness is the minimum viable acceptance book: benchmark plus held
    # underliers. Starter hedge/context symbols, CBOE indices, and configured
    # fallback research listings remain useful optional coverage; a missing
    # entitlement there must not trap a first user in Setup forever.
    benchmark = getattr(settings, "benchmark", "SPY")
    if portfolio_discovery_failed:
        try:
            required_symbols = store.read_required_symbols()
        except Exception:
            # Preserve unreadable evidence: Setup already reports it corrupt.
            pass
        else:
            if benchmark not in required_symbols:
                required_symbols.insert(0, benchmark)
            if PORTFOLIO_DISCOVERY_FAILURE_SYMBOL not in required_symbols:
                required_symbols.append(PORTFOLIO_DISCOVERY_FAILURE_SYMBOL)
            store.write_required_symbols(required_symbols)
    else:
        required_symbols = list(dict.fromkeys([benchmark, *held_symbols]))
        store.write_required_symbols(required_symbols)

    from quantmind.sources.fred import sync_fred

    try:
        for name, last in sync_fred(store).items():
            print(f"{name:>14} series through {last}")
    except Exception as exc:
        warnings.append("FRED macro data incomplete")
        print(
            f"WARNING: FRED macro sync unavailable ({type(exc).__name__}: {exc}); "
            "other cache phases remain usable"
        )

    if warnings:
        print(f"SYNC_RESULT: partial · {'; '.join(warnings)}")
    else:
        print("SYNC_RESULT: complete")


async def main(symbols: list[str]) -> None:
    """Run one sync and always release any created IBKR session."""
    ib_connections: list = []
    settings = Settings()
    with exclusive_sync_lock(settings.data_dir):
        try:
            await _run_main(symbols, ib_connections)
        finally:
            for ib in ib_connections:
                try:
                    ib.disconnect()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_UNIVERSE))
