# Personal World desk: engineering brief

Release: **0.6.0.0** · 5 September 2026

QuantMind now has a working, portfolio-aware world-monitoring workspace. It
answers “what deserves my attention, and why?” alongside the existing price,
options and risk tools. This is the ingestion and personal-context foundation
of a terminal, not a claim of Bloomberg data coverage or analytical parity.

## What you can use

Open **World**, click **Refresh sources**, and save watch symbols, interests
and regions. Start with `NVDA, ASML`, `semiconductors, energy`, and `Europe, US`
if those fit your interests. Select **My lens** to keep only explained matches.
Search, topic and source filters narrow the same cached view.

To use actual holdings, navigate from a pinned Portfolio to World, or apply
its 12-character snapshot reference. The server validates the selected account,
paper/live mode and base currency before using its symbols. An unpinned watchlist
is never labeled a portfolio. Existing navigation and the command palette now
carry the selected book between workspaces.

The source catalog contains 17 implemented adapters: 14 public feeds without
API keys, SEC releases with a contact identity, and explicitly enabled X and
Reddit connectors. See the [source matrix and configuration guide](data-sources.md)
for exact endpoints, access conditions, refresh cadence and smoke-test results.

For a desk that keeps collecting while the browser is closed:

```bash
uv run python -m quantmind.world_cli --watch --interval 300
```

Or restrict collection to chosen public feeds:

```bash
uv run python -m quantmind.world_cli --watch --source fed --source ecb --source eia
```

Leave that process running. It respects each source's minimum refresh interval;
it is not a registered system service. Stop it with Ctrl-C. The browser checks
the local cache every 30 seconds, or every two seconds while refresh is active.

## Modules and integration

```text
Manual refresh / CLI
        │
        ▼
WorldService ── shared SQLite lease ── up to 4 provider requests
        │                                       │
        │                              validated, bounded events
        ▼                                       ▼
world.sqlite3 ◀────────────────── independent source transactions
        │
Cached GET + scoped book + saved lens
        │
        ▼
Explainable local matching ── World event stream + source-health rail
```

| Module | Responsibility |
| --- | --- |
| `world/sources.py` | Fixed endpoints, source identity, cadence and access requirements |
| `world/providers.py`, `world/urls.py` | Bounded HTTP, RSS/Atom/JSON parsing, URL validation, social authentication |
| `world/models.py` | Canonical event, profile and private configuration contracts |
| `world/store.py` | Local schema, transactions, retention, corruption checks and refresh ownership |
| `world/service.py` | Refresh coordination, failure isolation and cached snapshots |
| `world/relevance.py` | Pure holding/watchlist/interest/region matching with explicit reasons |
| `api/routers/world.py` | Existing authentication and book-scope integration, typed responses |
| `world_cli.py` | One-shot and continuous collection using the same service and lock |
| `web/src/pages/World.tsx` | Reading, filtering, lens editing, refresh and source health |

There are three API operations: cached `GET /api/world`, local profile
replacement with `PUT /api/world/profile`, and explicit
`POST /api/world/refresh`. OpenAPI and generated TypeScript types are updated.
No order-placement endpoint or broker permission was added.

## Trust and failure behavior

- World uses its own `QM_DATA_DIR/world.sqlite3`; it cannot overwrite portfolio,
  price, FX or option evidence. Source errors preserve last-good data and dates.
- Provider requests have bounded time, response size and concurrency. Refresh
  cooldowns, Retry-After and a cross-process lease control repeated requests.
- Invalid XML, malformed records, unsafe URLs, future or timezone-less supplied
  dates and corrupt cache rows are rejected. Missing dates are explicitly
  labeled **Observed**, and repeated retrieval does not manufacture recency.
- External titles/excerpts are plain text. Provider secrets stay server-side;
  your holdings are matched locally, not uploaded as publisher search queries.
- Amber means a direct holding match. Watchlist and thematic matches use the
  outside-world color. No match score claims an expected return or risk impact.
- The layout works from 320px to 3440px in the browser tests. Phones and short
  landscape windows remain read-only; controls require at least 768 × 600.

## Verification and review

Final local release checks: **1,594 backend tests passed** (8 skipped,
1 deselected), **133 frontend tests passed**, and **18 Chromium/WebKit browser
tests passed**. Frontend lint and the production build passed; existing
deprecation/runtime-fixture warnings and the large JavaScript chunk warning
remain. Provider tests were rerun after a test-only placeholder cleanup.

Tests cover parsers, API authentication and book scope, profile persistence,
source outages, retention, cache corruption, single-flight refresh, CLI exit
behavior and browser navigation. The release review also exercises simultaneous
OS-process lock acquisition and interrupted ownership recovery.

Browser checks use Chromium and WebKit, the real API with isolated synthetic
data, multiple display widths, and a short landscape viewport. Unit tests use
fixed fixtures and mocked network access; CI does not depend on feed uptime.
The [README screenshots](../README.md#build-your-personal-world-desk) were captured
from the running UI using the explicitly illustrative demo, not real portfolios.

Read-only live probes parsed 13 public feeds. GDELT timed out and demonstrated
isolated failure. SEC, X and Reddit were fixture-tested, not live-tested without
the required identity or credentials. No subscriptions were purchased.

## What remains toward the terminal vision

The next high-value work is structured macro releases and historical vintages,
company filings/13Fs, issuer-backed UCITS constituent holdings, and measured
portfolio risk-channel attribution. These require their own data contracts and
acceptance tests. A press-release feed is not a fundamentals database; an ETF
mention is not look-through exposure.

Also not included: realtime streaming guarantees, push alerts, social-content
deletion reconciliation, estimates/consensus, a multiuser hosted security model,
or redistribution rights. X is paid, Reddit requires approved access, and
public availability does not grant permission to republish a provider's data.
Existing factor-risk, FX and options backlog items remain open in
[TODOS](../TODOS.md); this release does not mark them complete.
