# QuantMind

**A local-first, options-aware portfolio risk workbench for investors who want to understand the book they actually own.**

QuantMind brings positions, factor exposure, scenario analysis, expected shortfall, options Greeks, and hedge exploration into one auditable workspace. It is designed for a concentrated, discretionary investor with a long-beta core and an options overlay, not for generic charting or trade execution.

> [!WARNING]
> QuantMind is pre-1.0 research software. It is read-only, does not submit orders, and is not investment advice. Verify all inputs, model assumptions, and outputs independently before making an investment decision.

![QuantMind overview: market regime, book state, overnight moves, tail risk, and rotation](docs/screenshots/today-desktop.png)

![QuantMind factor-risk analysis: regression, factor decomposition, attribution, rolling beta, and tail risk](docs/screenshots/risk-desktop.png)

*Screenshots use QuantMind's deterministic synthetic E2E dataset. They demonstrate the interface and never contain a real account or portfolio.*

## Why QuantMind

Most market tools know the market. QuantMind is intended to know your book:

- Pin an immutable representation of the current book before running a What-If or hedge calculation.
- Separate systematic factor exposure from idiosyncratic risk with regression, beta, variance decomposition, and return attribution.
- Treat equity positions and option overlays as one analytical problem, with Greeks, option-chain storage, scenario stress, and Monte Carlo tools.
- Keep the evidence local and explicit: as-of times, data freshness, book references, failure states, and source boundaries are visible rather than silently filled in.
- Work from an IBKR-connected cache when available, while keeping a deterministic synthetic mode for development and demos.

QuantMind does not try to replace TradingView, Koyfin, or an execution platform. It supplies the book-aware risk layer those products cannot provide.

## What is included today

| Area | Current capability |
| --- | --- |
| Book truth | Canonical book contract, immutable snapshots, provenance manifests, corruption detection, and explicit `book_ref` pinning |
| Factor risk | CAPM and multi-factor regression, rolling beta, alpha only when risk-free evidence exists, variance decomposition, and attribution |
| Risk | Historical expected shortfall, annualised volatility, horizon Monte Carlo, drawdown context, and scenario tooling |
| Options | Read-only IBKR option-chain ingestion seam, stored chains, book Greeks, and option-aware risk boundaries |
| Decisions | What-If analysis, hedge candidate ranking/sizing, leverage checks, and saved local scenarios |
| Market context | Regime, macro, rotation, instruments, news adapters, and a research-model lab |
| Data | IBKR-first daily-bar sync, explicit yfinance fallback allowlist, FRED macro data, Parquet/DuckDB cache |
| Product | FastAPI API, React 19 web client, generated OpenAPI types, and a dark professional alpha workbench |

The current release is a single-book alpha. Multi-book onboarding, wider vendor ingestion, production broker jobs, and the full SaaS layer are intentionally future work.

## Architecture

```text
IBKR Gateway / permitted public sources
                 │
                 ▼
      sync CLIs and source adapters
                 │
                 ▼
  local Parquet + DuckDB evidence cache
                 │
                 ▼
Python risk, factor, options, and hedge core
                 │
                 ▼
         FastAPI contract + OpenAPI
                 │
                 ▼
    React workbench / local browser UI
```

The analytical core is deliberately separated from I/O. `risk/`, `analytics/`, and `hedge/` contain pure calculation code; broker, source, datastore, and API layers carry integration concerns.

## Quick start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/)
- Optional: IBKR Gateway or TWS for live read-only data and portfolio access

### Run the local workbench

```bash
git clone https://github.com/ionutnodis/quant-mind.git
cd quant-mind

uv sync --locked --dev
cd web && bun install --frozen-lockfile && cd ..

# Starts FastAPI at http://127.0.0.1:8000.
# If web/dist exists, this process also serves the built web client.
uv run python -m quantmind.api.main
```

For frontend development, run Vite in a second terminal:

```bash
cd web
bun run dev
```

Open the Vite URL, normally `http://127.0.0.1:5173`. Vite proxies `/api` to the local API, so the browser does not need direct broker access.

### Populate data

With IBKR Gateway running, sync the starter universe and macro series:

```bash
uv run python -m quantmind.sync_cli
```

To sync a smaller set of daily bars:

```bash
uv run python -m quantmind.sync_cli SPY QQQ TLT
```

Option-chain sync is a separate, explicit action. It reads cached spot data and snapshots monthly options up to 90 days out, within ±15% of spot:

```bash
uv run python -m quantmind.options_sync_cli SPY QQQ
```

If a broker is unavailable, the application starts in a degraded but honest state: unavailable live data is surfaced as unavailable rather than invented.

## Configuration

Runtime configuration is loaded from `.env` with the `QM_` prefix. Common settings:

```dotenv
# IBKR Gateway is paper-trading port 4002 by default; live is commonly 4001.
QM_HOST=127.0.0.1
QM_PORT=4002
QM_CLIENT_ID=17
QM_ACCOUNT_ID=

# Local data cache and benchmark
QM_DATA_DIR=data
QM_BENCHMARK=SPY

# Opt-in free fallback. IBKR remains authoritative for symbols it supplies.
QM_YFINANCE_SYMBOLS=EZU,EWU

# Optional local API protection
QM_API_TOKEN=
QM_API_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5173,http://localhost:5173
```

Do not commit `.env`, API keys, brokerage credentials, account numbers, or private portfolio data.

## Development and verification

```bash
# Python suite
uv run pytest

# Web suite, build, and browser smoke test
cd web
bun run lint
bunx vitest run
bun run build
bunx playwright test
```

The browser suite starts an isolated synthetic FastAPI cache. It is safe to use in CI and does not read a developer's local holdings.

When changing an API contract, regenerate both committed API artifacts:

```bash
uv run python scripts/dump_openapi.py
cd web
bun run gen:types
```

## Product boundaries

- **Read-only by design:** no order submission or broker execution surface.
- **Local-first:** binds to loopback by default and keeps the evidence cache on the local machine.
- **Single provenance:** yfinance fallback is opt-in and never silently replaces IBKR data for the same symbol.
- **Data honesty:** staleness, absent risk-free inputs, missing option data, and invalid numeric input are represented explicitly.
- **Responsive workflow:** the approved product policy targets one semantic UI from wide monitors to phones, with full authoring at 768px × 600px or larger and a read-only companion below that threshold. The current alpha is best used on a desktop or laptop while the responsive-shell implementation is completed.

## Documentation

- [Design system and product decisions](DESIGN.md)
- [Contributor and engineering notes](CLAUDE.md)
- [Web-client setup and API type generation](web/README.md)
- [API contract](openapi.json)
- [Release notes](CHANGELOG.md)
- [Deferred work](TODOS.md)

## License and status

QuantMind is currently an invite-only, pre-1.0 project. No open-source license has been granted yet; all rights are reserved unless the repository owner grants permission in writing.
