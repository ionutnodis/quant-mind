# QuantMind web client

The React 19 client is the local-first interface for QuantMind's book, factor-risk,
scenario, options, news, and research workflows. It talks only to the local FastAPI
service through `/api`; Vite proxies that path in development.

## Run locally

From the repository root, install Python dependencies and start the API:

```bash
uv sync --locked --dev
uv run python -m quantmind.api.main
```

In a second terminal, start the web client:

```bash
cd web
bun install --frozen-lockfile
bun run dev
```

Open the Vite URL (normally `http://127.0.0.1:5173`). The development proxy targets
`http://127.0.0.1:8000` by default. Set `QM_API_PROXY_TARGET` only when using another
local API address. If `QM_API_TOKEN` is configured for the API, set `VITE_QM_TOKEN` to
the same value before starting Vite.

## Verify changes

```bash
cd web
bun run lint
bunx vitest run
bun run build
bunx playwright test
```

`bun run build` type-checks the client and creates `web/dist`. The FastAPI server serves
that repository-relative bundle when it is present, so daily local use can run through the
API alone. Set `QM_WEB_DIST` only when a deployment keeps the built bundle elsewhere.

## API types

`src/lib/api-types.ts` is generated from the committed repository-level
[`openapi.json`](../openapi.json). After changing a request or response contract:

```bash
uv run python scripts/dump_openapi.py
cd web
bun run gen:types
```

Commit both generated files. `tests/test_openapi_drift.py` and CI reject a stale API
contract or generated type file.
