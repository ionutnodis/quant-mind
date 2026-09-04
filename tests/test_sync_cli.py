"""Task A2: universe/config wiring in sync_cli — pure data, no I/O. The
async `main()` orchestration itself talks to a live IB Gateway and is
exercised by the opt-in E2E test, not here."""

import quantmind.sync_cli as sync_cli
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


def _wire_sync_main(monkeypatch, tmp_path, portfolio_result, *, option_failure=None):
    calls = {}
    ib = FakeIB()

    class FakeSettings:
        data_dir = tmp_path
        host = "127.0.0.1"
        port = 4002
        client_id = 17
        account_id = "U111"

        @staticmethod
        def yfinance_symbol_list():
            return []

    class FakeBroker:
        def __init__(self, _ib, account_id):
            assert account_id == "U111"

        async def get_portfolio(self):
            if isinstance(portfolio_result, Exception):
                raise portfolio_result
            return portfolio_result

    async def sync_daily(_store, _broker, symbols, **kwargs):
        calls["daily"] = symbols
        calls["known_con_ids"] = kwargs["known_con_ids"]
        return {}

    async def sync_indexes(*_args, **_kwargs):
        return {}

    async def sync_metadata(*_args, **_kwargs):
        calls["metadata"] = True

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

    import quantmind.sources.fred as fred

    monkeypatch.setattr(fred, "sync_fred", lambda _store: {})
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

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY", "NVDA", "MU"]
    assert calls["known_con_ids"] == {"NVDA": 1}
    assert calls["options"] == ["MU"]
    assert [position.con_id for position in calls["held_contracts"]] == [2]
    assert calls["metadata"] is True
    assert ib.disconnected is True
    assert capsys.readouterr().out.strip().endswith("SYNC_RESULT: complete")


async def test_main_falls_back_to_the_requested_universe_when_portfolio_read_fails(
    monkeypatch, tmp_path, capsys
):
    calls, _ib = _wire_sync_main(monkeypatch, tmp_path, RuntimeError("portfolio failed"))

    await sync_cli.main(["SPY"])

    assert calls["daily"] == ["SPY"]
    assert "options" not in calls
    output = capsys.readouterr().out
    assert "syncing starter universe only" in output
    assert output.strip().endswith("SYNC_RESULT: partial · live portfolio unavailable")


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
