"""Serve the API (and, when built, the web app): `uv run python -m quantmind.api.main`.

Binds 127.0.0.1 only (Engineering Constraint 5). On startup we TRY to connect
the live broker (paper Gateway) so /api/portfolio shows the real book; failure
degrades to broker=None and live-broker reads report unavailable — the app
never depends on the Gateway being up (staleness policy).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from quantmind.api.app import create_app
from quantmind.broker.ib_broker import AccountSelectionError, IbBroker
from quantmind.config import Settings


class SPAStaticFiles(StaticFiles):
    """Serve built files, falling back to the SPA entry point for UI routes."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            client_path = path.lstrip("/")
            is_api_path = client_path == "api" or client_path.startswith("api/")
            is_asset_path = client_path == "assets" or client_path.startswith("assets/")
            has_file_extension = bool(Path(client_path).suffix)
            if exc.status_code != 404 or is_api_path or is_asset_path or has_file_extension:
                raise
            return await super().get_response("index.html", scope)


def default_web_dist() -> Path:
    """Return the repository-relative frontend build directory."""
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def broker_mode_for_port(port: int) -> str:
    """Classify the standard IB Gateway and TWS socket ports for Setup."""
    if port in {4002, 7497}:
        return "paper"
    if port in {4001, 7496}:
        return "live"
    return "custom"


def build():
    settings = Settings()
    from quantmind.datastore.store import BarStore

    app = create_app(
        store=BarStore(settings.data_dir),
        benchmark=settings.benchmark,
        base_currency=getattr(settings, "base_currency", "USD"),
        api_token=settings.api_token,
        allowed_origins=settings.api_allowed_origin_list(),
    )
    app.state.broker_connection_status = "connecting"
    app.state.broker_mode = broker_mode_for_port(settings.port)

    @asynccontextmanager
    async def lifespan(_app):
        ib = None
        broker = None
        connected_event = None
        connected_handler = None
        disconnect_event = None
        disconnect_handler = None
        try:
            from ib_async import IB

            from quantmind.broker.connection import ConnectionManager
            ib = IB()
            mgr = ConnectionManager(
                ib, host=settings.host, port=settings.port,
                client_id=settings.client_id + 1,  # distinct clientId from sync CLI
                max_attempts=2, backoff_base=0.5,
            )
            await mgr.ensure_connected()
            broker = IbBroker(ib, account_id=settings.account_id)
            # Validate one-account selection before readiness turns green.
            # This is a read-only positions request and prevents an advisor
            # session from silently blending multiple client accounts.
            await broker.get_portfolio()
            _app.state.broker_account_id = getattr(
                broker, "selected_account_id", settings.account_id or None
            )

            def mark_broker_disconnected(*_args) -> None:
                _app.state.broker = None
                _app.state.broker_connection_status = "unavailable"
                _app.state.broker_connection_error = "broker_disconnected"

            def mark_broker_reconnected(*_args) -> None:
                # ib_async emits this only after API startup completes. The
                # broker remains account-scoped and its streaming portfolio
                # will refresh through the same connection object.
                _app.state.broker = broker
                _app.state.broker_connection_status = "connected"
                _app.state.broker_connection_error = None

            disconnect_event = getattr(ib, "disconnectedEvent", None)
            if disconnect_event is not None:
                disconnect_handler = mark_broker_disconnected
                disconnect_event += disconnect_handler
            connected_event = getattr(ib, "connectedEvent", None)
            if connected_event is not None:
                connected_handler = mark_broker_reconnected
                connected_event += connected_handler
            _app.state.broker = broker
            _app.state.broker_connection_status = "connected"
            _app.state.broker_connection_error = None
            print("broker: connected to Gateway")
        except Exception as exc:  # degrade, never block startup
            _app.state.broker = None
            _app.state.broker_connection_status = "unavailable"
            _app.state.broker_connection_error = (
                "account_selection_required"
                if isinstance(exc, AccountSelectionError)
                else type(exc).__name__
            )
            print(f"broker: unavailable ({type(exc).__name__}) — live broker views unavailable")
        yield
        if disconnect_event is not None and disconnect_handler is not None:
            try:
                disconnect_event -= disconnect_handler
            except Exception:
                pass
        if connected_event is not None and connected_handler is not None:
            try:
                connected_event -= connected_handler
            except Exception:
                pass
        try:
            if ib is not None:
                ib.disconnect()
        except Exception:
            pass

    app.router.lifespan_context = lifespan

    dist = getattr(settings, "web_dist", None) or default_web_dist()
    if dist.exists():
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="web")
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
