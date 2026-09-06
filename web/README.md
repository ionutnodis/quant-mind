# QuantMind web client

The React 19 client is the local-first interface for QuantMind's book, factor-risk,
scenario, options, World monitoring, news, and research workflows. It talks only to the local FastAPI
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
bun run test:bundle
bunx playwright test
```

`bun run build` type-checks the client and creates `web/dist`. The FastAPI server serves
that repository-relative bundle when it is present, so daily local use can run through the
API alone. Set `QM_WEB_DIST` only when a deployment keeps the built bundle elsewhere.

`bun run test:bundle` compiles in memory and checks the complete static import graph:
the initial shell must stay below 500 kB raw / 150 kB gzip, Today/World/Setup must
not eagerly import Plotly, and all nine pages must remain separately loadable.
CI runs the same gate. Playwright builds and serves the production client against
the isolated synthetic API, including slow/failed page and chart download cases.

Pages load on demand. Today's candle, yield-spread, and correlation charts only
download their code when there is data to render; opening instrument metadata
does not download charts unless candles are present. Plotly remains a shared
deferred chunk (about 1.37 MB raw), so Vite still reports its 500 kB warning.
That warning is not a build failure, and its threshold has not been increased.
Loading and failure states stay inside the affected page/chart. If a module
download fails, navigation remains usable; **Reload page** retries explicitly
and warns that unsaved edits will be lost. There is no automatic reload loop.
Dynamic JavaScript uses native imports rather than extra modulepreload hints to
avoid [WebKit's failed-preload cache issue](https://bugs.webkit.org/show_bug.cgi?id=270357).
Initial HTML preloads and on-demand page styles are retained.

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
