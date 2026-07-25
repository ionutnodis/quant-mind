"""Serve the API (and, when built, the web app): `uv run python -m quantmind.api.main`.

Binds 127.0.0.1 only (Engineering Constraint 5). Token comes from QM_API_TOKEN;
empty means tokenless local dev.
"""

from __future__ import annotations

import uvicorn
from fastapi.staticfiles import StaticFiles

from quantmind.api.app import create_app
from quantmind.config import Settings


def build():
    settings = Settings()
    from quantmind.datastore.store import BarStore

    app = create_app(
        store=BarStore(settings.data_dir),
        benchmark=settings.benchmark,
        api_token=getattr(settings, "api_token", ""),
    )
    dist = settings.data_dir.parent / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=8000)
