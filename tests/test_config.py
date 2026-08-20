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


def test_yfinance_symbol_list_defaults_empty():
    s = Settings(_env_file=None)
    assert s.yfinance_symbol_list() == []


def test_yfinance_symbol_list_parses_comma_separated_env(monkeypatch):
    monkeypatch.setenv("QM_YFINANCE_SYMBOLS", "EZU, EWU ,MCHI")
    s = Settings(_env_file=None)
    assert s.yfinance_symbol_list() == ["EZU", "EWU", "MCHI"]


def test_api_security_settings_parse_from_environment(monkeypatch):
    monkeypatch.setenv("QM_API_TOKEN", "runtime-secret")
    monkeypatch.setenv(
        "QM_API_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000, http://localhost:5173 ",
    )
    s = Settings(_env_file=None)
    assert s.api_token == "runtime-secret"
    assert s.api_allowed_origin_list() == (
        "http://127.0.0.1:8000",
        "http://localhost:5173",
    )


def test_main_build_wires_security_settings(monkeypatch, tmp_path):
    import quantmind.api.main as main

    captured = {}

    class FakeSettings:
        data_dir = tmp_path / "data"
        benchmark = "SPY"
        api_token = "runtime-secret"
        host = "127.0.0.1"
        port = 4002
        client_id = 17

        @staticmethod
        def api_allowed_origin_list():
            return ("http://127.0.0.1:8000",)

    class FakeRouter:
        lifespan_context = None

    class FakeApp:
        router = FakeRouter()

        def mount(self, *_args, **_kwargs):
            raise AssertionError("no frontend dist should exist in this isolated test")

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return FakeApp()

    monkeypatch.setattr(main, "Settings", FakeSettings)
    monkeypatch.setattr(main, "create_app", fake_create_app)

    main.build()

    assert captured["api_token"] == "runtime-secret"
    assert captured["allowed_origins"] == ("http://127.0.0.1:8000",)
