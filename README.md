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

For the first private user, follow the complete [first-user runbook](docs/FIRST_USER_RUNBOOK.md). The short path is below.

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/)
- Optional: IBKR Gateway or TWS for live read-only data and portfolio access

### Run the local workbench

```bash
git clone https://github.com/ionutnodis/quant-mind.git
cd quant-mind

cp .env.example .env
uv sync --locked --dev
cd web && bun install --frozen-lockfile && bun run build && cd ..

# Start IBKR Gateway or TWS first, then start the local workbench.
uv run python -m quantmind.api.main
```

Open `http://127.0.0.1:8000/book/setup` (`/setup` remains a first-use alias). The Setup screen diagnoses the local API, the selected IBKR account, daily-bar freshness, and the current pinned book. It can run the market sync and pin the current live book without submitting orders.

For frontend development, run Vite in a second terminal:

```bash
cd web
bun run dev
```

Open the Vite URL, normally `http://127.0.0.1:5173`. Vite proxies `/api` to the local API, so the browser does not need direct broker access.

### Populate data

With IBKR Gateway running, sync the starter universe, the selected account's stock and option underliers, held-option chains, and macro series:

```bash
uv run python -m quantmind.sync_cli
```

To sync a smaller set of daily bars:

```bash
uv run python -m quantmind.sync_cli SPY QQQ TLT
```

Option-chain sync can also be run explicitly. It reads cached spot data and snapshots monthly options up to 90 days out, within ±15% of spot:

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
# Optional when the production frontend is built outside ./web/dist
# QM_WEB_DIST=/absolute/path/to/web/dist

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
- **Exactly one account:** `QM_ACCOUNT_ID` selects the portfolio. A multi-account IBKR session without an explicit selection fails closed rather than blending client books.
- **Advisor-safe reads:** selected-account portfolio updates avoid IBKR's global positions request, which is not available to advisor/master sessions with more than 50 subaccounts.
- **Local-first:** binds to loopback by default and keeps the evidence cache on the local machine.
- **Single provenance:** yfinance fallback is opt-in and never silently replaces IBKR data for the same symbol.
- **Data honesty:** readiness uses the weakest required market/macro observation; stale books, incomplete option chains, unsupported contracts, and invalid numeric input are represented explicitly. If any position is unpriced, QuantMind withholds total value and portfolio weights and labels the priced subtotal.
- **Currency guard:** the private alpha requires authoritative USD instrument identity and USD-denominated account totals until dated FX normalization is implemented; it never adds unlike local-currency prices.
- **Instrument guard:** the first-user acceptance book may contain stocks, ETFs, and equity options. Futures, futures options, bonds, CFDs, FX/cash rows, and other security types remain explicitly unsupported.
- **Responsive workflow:** one semantic UI scales from wide monitors through laptops and tablets to a read-only phone companion; authoring controls require at least 768 × 600 and dense analytical tables scroll inside their panels.

## Documentation

- [Design system and product decisions](DESIGN.md)
- [First-user installation and acceptance runbook](docs/FIRST_USER_RUNBOOK.md)
- [Contributor and engineering notes](CLAUDE.md)
- [Web-client setup and API type generation](web/README.md)
- [API contract](openapi.json)
- [Release notes](CHANGELOG.md)
- [Deferred work](TODOS.md)

## License and status

QuantMind is currently an invite-only, pre-1.0 project. No open-source license has been granted yet; all rights are reserved unless the repository owner grants permission in writing.
