"""Production-build serving contracts for the FastAPI host."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import quantmind.api.main as api_main


@pytest.fixture
def production_client(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    dist = runtime_dir / "web" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>QUANTMIND_PRODUCTION_BUILD</body></html>",
        encoding="utf-8",
    )
    (assets / "app-test.js").write_text(
        "window.__QUANTMIND_BUILD__ = true;",
        encoding="utf-8",
    )

    monkeypatch.setenv("QM_DATA_DIR", str(runtime_dir / "data"))
    monkeypatch.setenv("QM_WEB_DIST", str(dist))
    monkeypatch.setenv("QM_API_TOKEN", "")
    app = api_main.build()
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("path", ["/book/setup", "/setup", "/portfolio"])
def test_production_spa_deep_links_serve_built_index(production_client, path):
    response = production_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "QUANTMIND_PRODUCTION_BUILD" in response.text


def test_production_built_asset_is_served(production_client):
    response = production_client.get("/assets/app-test.js")

    assert response.status_code == 200
    assert response.text == "window.__QUANTMIND_BUILD__ = true;"


def test_unknown_api_route_remains_404(production_client):
    response = production_client.get("/api/unknown")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path",
    [
        "/assets/missing.js",
        "/assets/missing",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/missing.css",
    ],
)
def test_missing_static_files_remain_404(production_client, path):
    response = production_client.get(path)

    assert response.status_code == 404
    assert "QUANTMIND_PRODUCTION_BUILD" not in response.text


def test_data_dir_does_not_select_web_dist(tmp_path, monkeypatch):
    data_parent = tmp_path / "runtime"
    misleading_dist = data_parent / "web" / "dist"
    misleading_dist.mkdir(parents=True)
    (misleading_dist / "index.html").write_text("WRONG_BUILD", encoding="utf-8")
    package_dist = tmp_path / "package-web" / "dist"
    package_dist.mkdir(parents=True)
    (package_dist / "index.html").write_text("PACKAGE_BUILD", encoding="utf-8")

    monkeypatch.setenv("QM_DATA_DIR", str(data_parent / "data"))
    monkeypatch.delenv("QM_WEB_DIST", raising=False)
    monkeypatch.setenv("QM_API_TOKEN", "")
    monkeypatch.setattr(api_main, "default_web_dist", lambda: package_dist)
    app = api_main.build()

    response = TestClient(app, base_url="http://127.0.0.1").get("/")

    assert response.status_code == 200
    assert response.text == "PACKAGE_BUILD"
