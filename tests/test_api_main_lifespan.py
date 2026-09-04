"""Hermetic coverage for the broker-readiness startup boundary."""

from __future__ import annotations

import pytest

import quantmind.api.main as api_main
from quantmind.broker.ib_broker import AccountSelectionError
from quantmind.portfolio import Portfolio


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self):
        for handler in tuple(self.handlers):
            handler()


class FakeIB:
    def __init__(self):
        self.disconnected = False
        self.connectedEvent = FakeEvent()
        self.disconnectedEvent = FakeEvent()

    def disconnect(self):
        self.disconnected = True
        self.disconnectedEvent.emit()


class FakeConnectionManager:
    def __init__(self, ib, **_kwargs):
        self.ib = ib

    async def ensure_connected(self):
        return None


def _settings(tmp_path, *, account_id: str = "U222"):
    class FakeSettings:
        data_dir = tmp_path / "data"
        benchmark = "SPY"
        api_token = ""
        host = "127.0.0.1"
        port = 4002
        client_id = 17

        @staticmethod
        def api_allowed_origin_list():
            return ("http://127.0.0.1:8000",)

    FakeSettings.account_id = account_id
    return FakeSettings


def _wire_startup(monkeypatch, tmp_path, broker_type, *, account_id: str = "U222"):
    import ib_async
    import quantmind.broker.connection as connection

    monkeypatch.setattr(api_main, "Settings", _settings(tmp_path, account_id=account_id))
    monkeypatch.setattr(api_main, "IbBroker", broker_type)
    monkeypatch.setattr(ib_async, "IB", FakeIB)
    monkeypatch.setattr(connection, "ConnectionManager", FakeConnectionManager)
    return api_main.build()


async def test_lifespan_marks_broker_connected_only_after_validating_selected_account(
    monkeypatch, tmp_path
):
    captured = {}

    class Broker:
        def __init__(self, ib, account_id):
            captured["account_id"] = account_id
            captured["ib"] = ib

        async def get_portfolio(self):
            captured["validated"] = True
            return Portfolio(positions=(), as_of="2026-09-04")

    app = _wire_startup(monkeypatch, tmp_path, Broker)
    assert app.state.broker_connection_status == "connecting"

    async with app.router.lifespan_context(app):
        assert captured["account_id"] == "U222"
        assert captured["validated"] is True
        assert app.state.broker_connection_status == "connected"
        assert app.state.broker_connection_error is None

    assert captured["ib"].disconnected is True


async def test_lifespan_revokes_connected_readiness_when_ibkr_disconnects(
    monkeypatch, tmp_path
):
    captured = {}

    class Broker:
        def __init__(self, ib, account_id):
            captured["ib"] = ib
            assert account_id == "U222"

        async def get_portfolio(self):
            return Portfolio(positions=(), as_of="2026-09-04")

    app = _wire_startup(monkeypatch, tmp_path, Broker)

    async with app.router.lifespan_context(app):
        assert app.state.broker_connection_status == "connected"

        captured["ib"].disconnectedEvent.emit()

        assert app.state.broker is None
        assert app.state.broker_connection_status == "unavailable"
        assert app.state.broker_connection_error == "broker_disconnected"

        captured["ib"].connectedEvent.emit()

        assert app.state.broker is not None
        assert app.state.broker_connection_status == "connected"
        assert app.state.broker_connection_error is None


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (AccountSelectionError("choose one account"), "account_selection_required"),
        (RuntimeError("gateway failed"), "RuntimeError"),
    ],
)
async def test_lifespan_degrades_to_an_explicit_unavailable_state(
    monkeypatch, tmp_path, failure, expected_error
):
    class Broker:
        def __init__(self, _ib, account_id):
            assert account_id == "U222"

        async def get_portfolio(self):
            raise failure

    app = _wire_startup(monkeypatch, tmp_path, Broker)

    async with app.router.lifespan_context(app):
        assert app.state.broker is None
        assert app.state.broker_connection_status == "unavailable"
        assert app.state.broker_connection_error == expected_error
