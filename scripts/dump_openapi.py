"""Dump the FastAPI OpenAPI spec to `openapi.json` at the repo root.

The committed spec is the source of truth for `bun run gen:types`
(openapi-typescript, generates web/src/lib/api-types.ts) and is checked for
drift by tests/test_openapi_drift.py. Run this + `bun run gen:types`
whenever a router changes its request/response shape:

    uv run python scripts/dump_openapi.py
    cd web && bun run gen:types

Uses a fixture-free app (empty tmp-dir store, no broker, no token) — the
spec shape only depends on route/model definitions, never on cached data.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from quantmind.api.app import create_app
from quantmind.datastore.store import BarStore

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "openapi.json"


def build_spec() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(store=BarStore(Path(tmp)), benchmark="SPY")
        return app.openapi()


def main() -> None:
    spec = build_spec()
    OUTPUT_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
