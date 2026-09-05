"""Task A2: universe/config wiring in sync_cli. The
async `main()` orchestration itself talks to a live IB Gateway and is
exercised by the opt-in E2E test, not here."""

import pandas as pd

import quantmind.sync_cli as sync_cli
from quantmind.api.routers.setup import _market_data_status
from quantmind.datastore.store import BarMeta
from quantmind.portfolio import Portfolio, Position
from quantmind.sync_cli import DEFAULT_UNIVERSE, INDEX_UNIVERSE, WORLD_ETF_REGIONS


def test_world_etfs_and_sh_are_in_default_universe():
    for symbol in ["EZU", "EWU", "EWY", "EWT", "INDA", "MCHI", "EWZ", "EEM", "EFA", "SH"]:
        assert symbol in DEFAULT_UNIVERSE
        assert symbol in WORLD_ETF_REGIONS


def test_default_universe_has_no_duplicates():
    assert len(DEFAULT_UNIVERSE) == len(set(DEFAULT_UNIVERSE))


def test_every_world_etf_region_is_a_non_empty_string():
    for symbol, region in WORLD_ETF_REGIONS.items():
        assert isinstance(region, str) and len(region) > 0


def test_index_universe_has_vix_and_spx_on_cboe():
    assert INDEX_UNIVERSE == {"VIX": "CBOE", "SPX": "CBOE"}


def test_portfolio_sync_targets_include_stock_and_option_underliers_once():
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="NVDA", qty=100),
            Position(
                con_id=2,
                symbol="MU",
                qty=-5,
                sec_type="OPT",
                multiplier=100,
                strike=150,
                expiry="20261218",
                right="P",
            ),
            Position(con_id=3, symbol="NVDA", qty=-20),
            Position(con_id=4, symbol="USD", qty=1_000, sec_type="CASH"),
        ),
        as_of="2026-09-04",
    )

    daily, options = sync_cli.portfolio_sync_targets(portfolio)

    assert daily == ["NVDA", "MU"]
    assert options == ["MU"]


class FakeIB:
    disconnected = False

    def disconnect(self):
        self.disconnected = True


class FakeConnectionManager:
    def __init__(self, _ib, **_kwargs):
        pass

    async def ensure_connected(self):
        return None


def _wire_sync_main(
    monkeypatch,
    tmp_path,
    portfolio_result,
    *,
    option_failure=None,
    fx_failure=None,
    instrument_metadata=None,
    ucits_enabled=False,
    ucits_incomplete=False,
    ucits_failure=None,
    yfinance_symbols=None,
    account_currency=None,
    fred_failure=None,
):
    calls = {}
    ib = FakeIB()

    class FakeSettings:
        data_dir = tmp_path
        host = "127.0.0.1"
        port = 4002
        client_id = 17
        account_id = "U111"
        base_currency = "USD"
        ucits_metadata_enabled = ucits_enabled

        @staticmethod
        def yfinance_symbol_list():
            return list(yfinance_symbols or [])

    class FakeBroker:
        def __init__(self, _ib, account_id):
            assert account_id == "U111"

        async def get_portfolio(self):
            if isinstance(portfolio_result, Exception):
                raise portfolio_result
            return portfolio_result

        async def get_account_summary(self):
            if account_currency is None:
                raise RuntimeError("account summary unavailable")
            return {"currency": account_currency}

    async def sync_daily(_store, _broker, symbols, **kwargs):
        calls["daily"] = symbols
        calls["known_con_ids"] = kwargs["known_con_ids"]
        calls["option_contract_con_ids"] = kwargs["option_contract_con_ids"]
        return {}

    async def sync_indexes(*_args, **_kwargs):
        return {}

    async def sync_metadata(*_args, **_kwargs):
        calls["metadata"] = True
        return instrument_metadata or {}

    async def sync_options(*_args, underliers, held_contracts, **_kwargs):
        calls["options"] = underliers
        calls["held_contracts"] = held_contracts
        if option_failure is not None:
            raise option_failure
        return {symbol: 1 for symbol in underliers}

    monkeypatch.setattr(sync_cli, "Settings", FakeSettings)
    monkeypatch.setattr(sync_cli, "IB", lambda: ib)
    monkeypatch.setattr(sync_cli, "ConnectionManager", FakeConnectionManager)
    monkeypatch.setattr(sync_cli, "IbBroker", FakeBroker)
    monkeypatch.setattr(sync_cli, "sync_daily_bars", sync_daily)
    monkeypatch.setattr(sync_cli, "sync_index_bars", sync_indexes)
    monkeypatch.setattr(sync_cli, "sync_instrument_metadata", sync_metadata)
    monkeypatch.setattr(sync_cli, "sync_options_chains", sync_options)

    def sync_yfinance(store, _provider, symbols, **_kwargs):
        calls["yfinance"] = symbols
        for symbol in symbols:
            store.write_instrument_metadata(
                symbol,
                {"con_id": -1, "provider": "yfinance", "currency": "EUR"},
            )
        return ({symbol: -1 for symbol in symbols}, [])

    monkeypatch.setattr(sync_cli, "sync_yfinance_bars", sync_yfinance)
    monkeypatch.setattr(sync_cli, "YFinanceProvider", lambda: object())

    def sync_fx(_store, _provider, currencies, **_kwargs):
        calls["fx"] = sorted(currencies)
        if fx_failure is not None:
            raise fx_failure
        return type("Result", (), {"as_of": "2026-09-04"})()

    monkeypatch.setattr(sync_cli, "sync_ecb_fx", sync_fx, raising=False)
    monkeypatch.setattr(sync_cli, "EcbFxProvider", lambda: object(), raising=False)

    def sync_ucits(_store, metadata, _provider, **_kwargs):
        calls["ucits"] = metadata
        if ucits_failure is not None:
            raise ucits_failure
        return {
            symbol: type(
                "Status",
                (),
                {
                    "freshness": type(
                        "Fresh",
                        (),
                        {"value": "MISSING" if ucits_incomplete else "FRESH"},
                    )()
                },
            )()
            for symbol in metadata
            if fields_is_etf(metadata[symbol])
        }

    def fields_is_etf(fields):
        return fields.get("stock_type") == "ETF"

    monkeypatch.setattr(sync_cli, "sync_ucits_profiles", sync_ucits, raising=False)
    monkeypatch.setattr(sync_cli, "JustEtfProvider", lambda _store: object(), raising=False)

    import quantmind.sources.fred as fred

    def sync_fred(_store):
        if fred_failure is not None:
            raise fred_failure
        return {}

    monkeypatch.setattr(fred, "sync_fred", sync_fred)
    return calls, ib


async def test_main_adds_held_symbols_and_syncs_held_option_chains(monkeypatch, tmp_path, capsys):
    portfolio = Portfolio(
        positions=(
            Position(con_id=1, symbol="NVDA", qty=100),
            Position(con_id=2, symbol="MU", qty=-2, sec_type="OPT", multiplier=100),
        ),
        as_of="2026-09-04",
    )
    calls, ib = _wire_sync_main(monkeypatch, tmp_path, portfolio)
    sync_cli.BarStore(tmp_path).write_required_symbols(
        ["SPY", sync_cli.PORTFOLIO_DISCOVERY_FAILURE_SYMBOL]
    )

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY", "NVDA", "MU"]
    assert calls["known_con_ids"] == {"NVDA": 1}
    assert calls["option_contract_con_ids"] == {"MU": [2]}
    assert calls["options"] == ["MU"]
    assert [position.con_id for position in calls["held_contracts"]] == [2]
    assert calls["metadata"] is True
    assert ib.disconnected is True
    assert sync_cli.BarStore(tmp_path).read_required_symbols() == ["SPY", "NVDA", "MU"]
    assert capsys.readouterr().out.strip().endswith("SYNC_RESULT: complete")


async def test_main_falls_back_to_the_requested_universe_when_portfolio_read_fails(
    monkeypatch, tmp_path, capsys
):
    calls, _ib = _wire_sync_main(monkeypatch, tmp_path, RuntimeError("portfolio failed"))

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY"]
    assert "options" not in calls
    assert sync_cli.BarStore(tmp_path).read_required_symbols() == [
        "SPY",
        sync_cli.PORTFOLIO_DISCOVERY_FAILURE_SYMBOL,
    ]
    output = capsys.readouterr().out
    assert "syncing starter universe only" in output
    assert output.strip().endswith("SYNC_RESULT: partial · live portfolio unavailable")


async def test_failed_holdings_discovery_preserves_requirements_and_keeps_setup_incomplete(
    monkeypatch, tmp_path, capsys
):
    store = sync_cli.BarStore(tmp_path)
    store.write_required_symbols(["SPY", "ASML"])
    store.write_symbol_map({"SPY": 1, "ASML": 2})
    today = pd.Timestamp.now().normalize()
    bars = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1_000.0],
        },
        index=pd.DatetimeIndex([today]),
    )
    for symbol, con_id, currency in [("SPY", 1, "USD"), ("ASML", 2, "EUR")]:
        store.write_bars(
            con_id,
            "1d",
            bars,
            BarMeta(
                bar_type="ADJUSTED_LAST",
                adjusted_asof=today.date().isoformat(),
            ),
        )
        store.write_instrument_metadata(
            symbol,
            {"con_id": con_id, "currency": currency, "provider": "ibkr"},
        )
    assert _market_data_status(store, "SPY").status == "ready"
    _calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        RuntimeError("portfolio failed"),
    )

    await sync_cli.main(["SPY"])

    assert store.read_required_symbols() == [
        "SPY",
        "ASML",
        sync_cli.PORTFOLIO_DISCOVERY_FAILURE_SYMBOL,
    ]
    readiness = _market_data_status(store, "SPY")
    assert readiness.status == "incomplete"
    assert readiness.symbols == 2
    assert readiness.missing_symbols == []
    assert readiness.portfolio_discovery_error == "live_portfolio_unavailable"
    assert capsys.readouterr().out.strip().endswith(
        "SYNC_RESULT: partial · live portfolio unavailable"
    )


async def test_main_preserves_multiple_same_ticker_stock_identities(
    monkeypatch, tmp_path
):
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=12345,
                symbol="ASML",
                qty=5,
                currency="EUR",
                exchange="AEB",
            ),
            Position(
                con_id=99999,
                symbol="ASML",
                qty=3,
                currency="USD",
                exchange="NASDAQ",
            ),
        ),
        as_of="2026-09-04",
    )
    calls, _ib = _wire_sync_main(monkeypatch, tmp_path, portfolio)

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY", "ASML"]
    assert calls["known_con_ids"] == {"ASML": [12345, 99999]}


async def test_main_always_disconnects_when_an_unexpected_phase_error_occurs(
    monkeypatch, tmp_path
):
    _calls, ib = _wire_sync_main(
        monkeypatch, tmp_path, Portfolio(positions=(), as_of="2026-09-04")
    )

    async def explode(*_args, **_kwargs):
        raise RuntimeError("unexpected store failure")

    monkeypatch.setattr(sync_cli, "sync_daily_bars", explode)

    try:
        await sync_cli.main(["SPY"])
    except RuntimeError:
        pass

    assert ib.disconnected is True


async def test_option_chain_failure_does_not_discard_the_daily_bar_sync(
    monkeypatch, tmp_path, capsys
):
    portfolio = Portfolio(
        positions=(Position(con_id=2, symbol="MU", qty=-2, sec_type="OPT", multiplier=100),),
        as_of="2026-09-04",
    )
    calls, _ib = _wire_sync_main(
        monkeypatch, tmp_path, portfolio, option_failure=RuntimeError("no permission")
    )

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY", "MU"]
    assert calls["options"] == ["MU"]
    output = capsys.readouterr().out
    assert "daily bars remain usable" in output
    assert output.strip().endswith("SYNC_RESULT: partial · held-option chain unavailable")


async def test_main_syncs_ecb_fx_for_held_non_base_currencies(monkeypatch, tmp_path, capsys):
    portfolio = Portfolio(
        positions=(
            Position(con_id=7, symbol="IWDA", qty=20, currency="EUR", exchange="AEB"),
            Position(con_id=8, symbol="ASML", qty=5, currency="EUR", exchange="AEB"),
        ),
        as_of="2026-09-04",
    )
    calls, _ib = _wire_sync_main(monkeypatch, tmp_path, portfolio)

    await sync_cli.main(["SPY"])

    assert calls["fx"] == ["EUR", "USD"]
    assert "ECB FX" in capsys.readouterr().out


async def test_main_includes_fallback_and_account_currencies_in_same_fx_sync(
    monkeypatch, tmp_path, capsys
):
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        yfinance_symbols=["IWDA.AS"],
        account_currency="HKD",
    )

    await sync_cli.main(["SPY"])

    assert calls["yfinance"] == ["IWDA.AS"]
    assert calls["fx"] == ["EUR", "HKD", "USD"]
    assert "ECB FX" in capsys.readouterr().out


async def test_fx_failure_is_partial_and_keeps_market_sync(monkeypatch, tmp_path, capsys):
    portfolio = Portfolio(
        positions=(Position(con_id=7, symbol="IWDA", qty=20, currency="EUR"),),
        as_of="2026-09-04",
    )
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        portfolio,
        fx_failure=RuntimeError("ECB unavailable"),
    )

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY", "IWDA"]
    output = capsys.readouterr().out
    assert "FX sync unavailable" in output
    assert output.strip().endswith("SYNC_RESULT: partial · FX reference rates unavailable")


async def test_main_runs_opt_in_ucits_enrichment_after_ibkr_metadata(monkeypatch, tmp_path):
    metadata = {
        "IWDA": {
            "provider": "ibkr",
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
        }
    }
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        instrument_metadata=metadata,
        ucits_enabled=True,
    )

    await sync_cli.main(["SPY"])

    assert calls["ucits"] == metadata


async def test_incomplete_ucits_profiles_are_partial_without_discarding_prices(
    monkeypatch, tmp_path, capsys
):
    metadata = {
        "IWDA": {
            "provider": "ibkr",
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
        }
    }
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        instrument_metadata=metadata,
        ucits_enabled=True,
        ucits_incomplete=True,
    )

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY"]
    output = capsys.readouterr().out
    assert "UCITS profiles unavailable or stale" in output
    assert output.strip().endswith("SYNC_RESULT: partial · UCITS profiles incomplete")


async def test_ucits_exception_is_partial_and_later_phases_still_run(
    monkeypatch, tmp_path, capsys
):
    metadata = {
        "IWDA": {
            "provider": "ibkr",
            "currency": "EUR",
            "stock_type": "ETF",
            "isin": "IE00B4L5Y983",
        }
    }
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        instrument_metadata=metadata,
        ucits_enabled=True,
        ucits_failure=RuntimeError("provider parser changed"),
        yfinance_symbols=["IWDA.AS"],
    )

    await sync_cli.main(["SPY"])

    assert calls["ucits"] == metadata
    assert calls["yfinance"] == ["IWDA.AS"]
    assert calls["fx"] == ["EUR", "USD"]
    output = capsys.readouterr().out
    assert "later cache phases remain usable" in output
    assert output.strip().endswith("SYNC_RESULT: partial · UCITS profiles incomplete")


async def test_fred_failure_is_partial_after_successful_market_sync(
    monkeypatch, tmp_path, capsys
):
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        fred_failure=RuntimeError("FRED unavailable"),
    )

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY"]
    output = capsys.readouterr().out
    assert "FRED macro sync unavailable" in output
    assert output.strip().endswith("SYNC_RESULT: partial · FRED macro data incomplete")


async def test_main_does_not_fetch_ucits_profiles_without_explicit_opt_in(monkeypatch, tmp_path):
    metadata = {
        "IWDA": {"provider": "ibkr", "stock_type": "ETF", "isin": "IE00B4L5Y983"}
    }
    calls, _ib = _wire_sync_main(
        monkeypatch,
        tmp_path,
        Portfolio(positions=(), as_of="2026-09-04"),
        instrument_metadata=metadata,
    )

    await sync_cli.main(["SPY"])

    assert "ucits" not in calls
