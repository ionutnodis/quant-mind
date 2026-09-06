from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from quantmind.api.app import create_app
from quantmind.datastore.store import BarStore


@pytest.fixture
def client(tmp_path):
    store = BarStore(tmp_path)
    store.write_symbol_map({"NVDA": 1, "ASML": 2})
    app = create_app(store, benchmark="SPY", api_token="testtoken")
    return TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer testtoken"})


def test_cached_empty_world_does_not_claim_universe_is_holdings(client):
    response = client.get("/api/world")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == [] and data["context"]["symbols"] == []
    assert data["as_of"] is None
    assert {s["id"] for s in data["sources"] if s["state"] == "disabled"} >= {"x", "reddit"}


def test_profile_persists_without_network_and_rejects_unknown_config(client):
    response = client.put("/api/world/profile", json={"watch_symbols": ["nvda", "NVDA"], "interests": ["Energy"]})
    assert response.status_code == 200
    assert response.json() == {"watch_symbols": ["NVDA"], "interests": ["energy"], "regions": []}
    assert client.get("/api/world").json()["profile"]["watch_symbols"] == ["NVDA"]
    assert client.put("/api/world/profile", json={"feed_url": "http://127.0.0.1"}).status_code == 422
    assert client.put("/api/world/profile", json={"interests": [str(i) for i in range(21)]}).status_code == 422


def test_world_auth_and_host_protection_matches_other_portfolio_routes(client):
    assert client.get("/api/world", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/world/refresh", headers={"Host": "evil.example"}).status_code == 403
    assert client.put("/api/world/profile", json={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_pin_reference_drives_context_and_invalid_reference_fails_closed(client):
    pin = client.post("/api/book/pin", json={"positions": [{"symbol": "NVDA", "qty": 2}]}).json()
    response = client.get("/api/world", params={"book_ref": pin["snapshot_id"]})
    assert response.status_code == 200
    assert response.json()["context"]["symbols"] == ["NVDA"]
    assert client.get("/api/world", params={"book_ref": "../../etc/passwd"}).status_code == 422
    assert client.get("/api/world", params={"book_ref": "abcdef012345"}).status_code == 422


def test_pinned_book_scope_is_checked_before_any_relevance(client):
    pin = client.post("/api/book/pin", json={"positions": [{"symbol": "NVDA", "qty": 2}]}).json()
    client.app.state.base_currency = "EUR"
    response = client.get("/api/world", params={"book_ref": pin["snapshot_id"]})
    assert response.status_code == 409
    assert "base currency" in response.json()["detail"]


def test_world_cache_failure_is_isolated_from_other_routes(client, monkeypatch):
    from quantmind.world.store import WorldStoreError
    def failed(*args, **kwargs):
        raise WorldStoreError("World cache unavailable")
    monkeypatch.setattr(client.app.state.world.cache, "profile", failed)
    response = client.get("/api/world")
    assert response.status_code == 503
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/book/current").status_code != 500


def test_refresh_api_returns_partial_results_and_cached_ranked_events(client):
    import httpx
    from quantmind.world.sources import SOURCES
    client.app.state.world.sources = SOURCES[:2]
    client.app.state.world.clock = lambda: datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    def handle(request):
        if "ecb" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=b'<rss><channel><item><title>Nvidia earnings</title><link>https://example.org/n</link><pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')
    client.app.state.world.transport = httpx.MockTransport(handle)
    client.put("/api/world/profile", json={"watch_symbols": ["NVDA"]})
    response = client.post("/api/world/refresh")
    assert response.status_code == 200
    assert response.json() == {"updated": 1, "failed": 1, "skipped": 0}
    data = client.get("/api/world").json()
    assert data["items"][0]["matched_symbols"] == ["NVDA"]
    assert data["sources"][1]["state"] == "error"


def test_world_configuration_is_server_only_and_secret_repr_is_redacted(monkeypatch):
    monkeypatch.setenv("QM_WORLD_X_BEARER_TOKEN", "private-world-token")
    monkeypatch.setenv("QM_WORLD_X_ENABLED", "true")
    monkeypatch.setenv("QM_WORLD_X_QUERY", "from:example")
    from quantmind.config import Settings
    settings = Settings(_env_file=None)
    config = settings.world_config()
    assert config.x_enabled and config.x_bearer_token.get_secret_value() == "private-world-token"
    assert "private-world-token" not in repr(settings)
