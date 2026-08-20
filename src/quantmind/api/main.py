"""Serve the API (and, when built, the web app): `uv run python -m quantmind.api.main`.

Binds 127.0.0.1 only (Engineering Constraint 5). On startup we TRY to connect
the live broker (paper Gateway) so /api/portfolio shows the real book; failure
degrades to broker=None and live-broker reads report unavailable — the app
never depends on the Gateway being up (staleness policy).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

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
        api_token=settings.api_token,
        allowed_origins=settings.api_allowed_origin_list(),
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            from ib_async import IB

            from quantmind.broker.connection import ConnectionManager
            from quantmind.broker.ib_broker import IbBroker

            ib = IB()
            mgr = ConnectionManager(
                ib, host=settings.host, port=settings.port,
                client_id=settings.client_id + 1,  # distinct clientId from sync CLI
                max_attempts=2, backoff_base=0.5,
            )
            await mgr.ensure_connected()
            _app.state.broker = IbBroker(ib)
            print("broker: connected to Gateway")
        except Exception as exc:  # degrade, never block startup
            _app.state.broker = None
            print(f"broker: unavailable ({type(exc).__name__}) — live broker views unavailable")
        yield
        try:
            ib.disconnect()
        except Exception:
            pass

    app.router.lifespan_context = lifespan

    dist = settings.data_dir.parent / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


if __name__ == "__main__":
    import os

    # QM_RELOAD=1 (default): watch src/ and restart on change — kills the
    # recurring stale-server failure mode (three field incidents: routes/
    # messages lagging the code because uvicorn predated the commits).
    if os.environ.get("QM_RELOAD", "1") == "1":
        uvicorn.run(
            "quantmind.api.main:build", factory=True,
            host="127.0.0.1", port=8000,
            reload=True, reload_dirs=["src"],
        )
    else:
        uvicorn.run(build(), host="127.0.0.1", port=8000)
