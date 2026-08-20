from fastapi.testclient import TestClient

from quantmind.testing.synthetic_e2e import build_synthetic_app


def test_synthetic_e2e_app_serves_fixed_market_data_from_its_own_store(tmp_path):
    fixture_root = tmp_path / "synthetic-cache"
    app = build_synthetic_app(fixture_root)
    client = TestClient(app, base_url="http://127.0.0.1")

    health = client.get("/api/health")
    brief = client.get("/api/brief")

    assert health.status_code == 200
    assert brief.status_code == 200
    body = brief.json()
    assert {tile["symbol"] for tile in body["tiles"]} == {"QQQ", "SPY"}
    assert body["as_of"] == "2026-07-24T00:00:00Z"
    assert (fixture_root / "symbols.json").exists()
    assert fixture_root != fixture_root.parent / "data"
