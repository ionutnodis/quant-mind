"""Drift gate: the committed openapi.json must match what the app generates
right now. This is the contract behind web/src/lib/api-types.ts (generated
via `bun run gen:types` from openapi.json) — if a router changes shape and
nobody regenerates, the frontend types silently lie. Catch that here instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ isn't a package on the default test path (no conftest.py rootdir
# insertion owned by this task) — add the repo root explicitly so the
# dump script's logic can be imported and reused rather than duplicated.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.dump_openapi import OUTPUT_PATH, build_spec  # noqa: E402

DRIFT_MESSAGE = (
    "openapi.json is stale relative to the current routers — "
    "run: uv run python scripts/dump_openapi.py && (cd web && bun run gen:types)"
)


def test_committed_openapi_json_matches_generated_spec():
    assert OUTPUT_PATH.exists(), DRIFT_MESSAGE
    committed = json.loads(OUTPUT_PATH.read_text())
    current = json.loads(json.dumps(build_spec(), sort_keys=True))
    assert committed == current, DRIFT_MESSAGE
