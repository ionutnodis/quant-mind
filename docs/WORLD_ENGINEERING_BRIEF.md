# Personal World desk: engineering brief

Release candidate: **0.6.0.0** · verified 6 September 2026

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
- Parsing runs off-loop with XML depth/node limits and bounded text inputs.
  Cancellation drains its worker before releasing capacity. One malformed
  record does not discard valid neighbors; a nonempty all-invalid batch records
  an error without advancing the last-good timestamp. Genuine empty feeds remain
  valid. [Exact ingestion limits and link policy](data-sources.md#cache-resilience-and-privacy).
- Invalid XML, malformed records, unsafe URLs, future or timezone-less supplied
  dates and corrupt cache rows are rejected. Missing dates are explicitly
  labeled **Observed**, and repeated retrieval does not manufacture recency.
- External titles/excerpts are plain text. Provider secrets stay server-side;
  your holdings are matched locally, not uploaded as publisher search queries.
- Amber means a direct holding match. Watchlist and thematic matches use the
  outside-world color. No match score claims an expected return or risk impact.
- The layout works from 320px to 3440px in the browser tests. Phones and short
  landscape windows remain read-only; controls require at least 768 × 600.
- Confirmed lens saves update cached profile state before editing is unlocked;
  older pending reads are cancelled. Failed reads cannot restore a superseded
  lens. Validation errors remain readable without serializing raw input/context,
  and uppercase book references are normalized before API requests.

## Verification and review

Latest local release-candidate checks: **1,756 backend tests passed on each of
Python 3.12.4 and 3.12.14** (8 intentionally unshipped T3 tests skipped, 1
live-IBKR test deselected), **156
frontend tests passed**, **36 Chromium/WebKit browser tests passed**, and **5
bundle-budget checks passed**. Locked installs, generated-type drift checks,
frontend lint, TypeScript and the production build passed. Six existing lint
warnings and five backend deprecation/runtime-fixture warnings remain.

The initial JavaScript dependency graph is **363,320 bytes raw / 114,036 bytes
gzip**; pages and charts load on demand. Plotly's deferred runtime remains about
1.37 MB raw and still emits Vite's large-chunk advisory. It is not downloaded
when opening an empty Today, World or Setup view.

The follow-up fixes added **145 backend cases, 14 frontend cases and 6 browser
cases** over the preceding verification. Regressions cover unsafe cached article
links, malformed feeds, all-invalid source health, cancellation, ticker boundaries,
saved-lens races and actual 44px link boxes on touch-enabled phones and tablets.
These are passing test counts, not a claim of complete line/branch coverage.

The original roughly 1 MB hostile XML fixture now fails with a safe complexity
error in **0.003 seconds**, versus **15.37 seconds** before; an unrelated 50ms
callback remained responsive. On the same bounded 420-event, 100-watch-symbol,
20-interest, 20-region and 10-holding synthetic fixture, median ranking CPU time
fell from **1.284 seconds to 0.325 seconds** across three runs (~3.95× faster).
This is a diagnostic CPU benchmark, not an HTTP latency or typical-user promise.
Run `uv run python scripts/world_ranking_benchmark.py --runs 3` to reproduce the
fixture. CI guards regex-operation counts and literal behavior, not wall-clock
ranking thresholds; the optimization adds no persisted or process-global result cache.

The first CI run exposed a prior-version literal in the Setup response test
after the release bump. The test now reads `VERSION`, and a new regression checks
the API, Python package, lockfile and web package against that manifest. The full
backend suite was rerun after the fix; no application behavior was weakened.

A subsequent Linux CI run exposed a Python patch-version difference: 3.12.14's
HTML parser tolerates unknown marked sections that 3.12.4 rejects. An explicit,
ASCII-keyword declaration policy now isolates those records consistently while
preserving recognized sections and valid neighboring stories. Regressions also
cover Unicode keyword lookalikes and record-local parser exceptions independently
of the standard library's grammar. Both actual Python versions are tested; CI
continues to use the latest available 3.12 patch rather than pinning an older one.

Tests cover parsers, API authentication and book scope, profile persistence,
source outages, retention, cache corruption, single-flight refresh, CLI exit
behavior and browser navigation. The release review also exercises simultaneous
OS-process lock acquisition and interrupted ownership recovery.

Browser checks use Chromium and WebKit, the real API with isolated synthetic
data, multiple display widths, and a short landscape viewport. Unit tests use
fixed fixtures and mocked network access; CI does not depend on feed uptime.
The [README screenshots](../README.md#make-world-your-own) were captured
from the running UI using the explicitly illustrative demo, not real portfolios.

These checks do not certify live brokerage connectivity or every provider's
current availability. The original World implementation's historical test-first
chronology remains unverified; the follow-up fixes recorded failing regressions
before their implementation. No order entry or main-branch merge is implied by
this release-candidate brief.

The initial read-only live probe parsed 13 public feeds. A final repeat parsed
11: BLS returned HTTP 403, BIS returned a non-feed XML response, and GDELT timed
out. Each demonstrated isolated failure without a bypass or hidden retry. SEC,
X and Reddit were fixture-tested, not live-tested without the required identity
or credentials. No subscriptions were purchased.

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
