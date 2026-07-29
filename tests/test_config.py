from quantmind.config import Settings


def test_defaults_are_local_paper_gateway():
    s = Settings(_env_file=None)
    s.host == "127.0.0.1"
    assert s.host == "127.0.0.1"
    assert s.port == 4002  # IB Gateway paper default
    assert s.benchmark == "SPY"


def test_env_override(monkeypatch):
    monkeypatch.setenv("QM_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("QM_CLIENT_ID", "23")
    s = Settings(_env_file=None)
    assert s.account_id == "DU1234567"
    assert s.client_id == 23


def test_base_currency_defaults_to_usd():
    s = Settings(_env_file=None)
    assert s.base_currency == "USD"


def test_base_currency_env_override(monkeypatch):
    # The real account is GBP-based (FX-aware valuation, TODOS 2026-07-27).
    monkeypatch.setenv("QM_BASE_CURRENCY", "GBP")
    s = Settings(_env_file=None)
    assert s.base_currency == "GBP"


def test_yfinance_symbol_list_defaults_empty():
    s = Settings(_env_file=None)
    assert s.yfinance_symbol_list() == []


def test_yfinance_symbol_list_parses_comma_separated_env(monkeypatch):
    monkeypatch.setenv("QM_YFINANCE_SYMBOLS", "EZU, EWU ,MCHI")
    s = Settings(_env_file=None)
    assert s.yfinance_symbol_list() == ["EZU", "EWU", "MCHI"]
