from __future__ import annotations

from contextlib import contextmanager

import pytest

import quantmind.options_sync_cli as options_sync_cli


@pytest.mark.asyncio
async def test_options_cli_holds_the_datastore_lock_for_the_entire_sync(
    tmp_path, monkeypatch
):
    events: list[str] = []

    class FakeSettings:
        data_dir = tmp_path
        host = "127.0.0.1"
        port = 4002
        client_id = 17

    class FakeIb:
        def disconnect(self):
            assert events[-1] == "synced"
            events.append("disconnected")

    class FakeConnectionManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def ensure_connected(self):
            assert events == ["locked"]
            events.append("connected")

    @contextmanager
    def fake_lock(data_dir):
        assert data_dir == tmp_path
        events.append("locked")
        try:
            yield
        finally:
            events.append("unlocked")

    async def fake_sync(*_args, **kwargs):
        assert events[-1] == "connected"
        assert kwargs["underliers"] == ["SPY"]
        events.append("synced")
        return {"SPY": 3}

    monkeypatch.setattr(options_sync_cli, "Settings", FakeSettings)
    monkeypatch.setattr(options_sync_cli, "IB", FakeIb)
    monkeypatch.setattr(
        options_sync_cli, "ConnectionManager", FakeConnectionManager
    )
    monkeypatch.setattr(options_sync_cli, "exclusive_sync_lock", fake_lock)
    monkeypatch.setattr(options_sync_cli, "sync_options_chains", fake_sync)

    await options_sync_cli.main(["SPY"])

    assert events == [
        "locked",
        "connected",
        "synced",
        "disconnected",
        "unlocked",
    ]
