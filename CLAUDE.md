# QuantMind

Local-first quant workbench: Python core (`src/quantmind/`), FastAPI backend, and React frontend. IBKR remains behind a read-only broker interface; the cache uses DuckDB/Parquet. Product and responsive-design decisions live in [DESIGN.md](DESIGN.md); the committed API contract is [openapi.json](openapi.json).

## Testing
- Backend: `uv run pytest`. E2E paper-Gateway tests are opt-in: `uv run pytest -m e2e --override-ini addopts=''` (needs IB Gateway on port 4002).
- Frontend: from `web/`, run `bunx vitest run`, `bun run build`, and `bunx playwright test`. See [web/README.md](web/README.md) for the local run and generated-type workflow.
- TDD is the law here: no production code without a failing test first. Risk-math modules test against hand-computed/golden values.

## Engineering constraints
- Pure core: `risk/`, `analytics/`, and `hedge/` remain calculation-focused and picklable; I/O belongs in `broker/`, `sources/`, `datastore/`, and the provider boundary in `fx.py`.
- Risk math uses ADJUSTED_LAST bars keyed by conId; adjusted history is refreshable, never append-only.
- Parquet is the source of truth; exactly one writer process; DuckDB readers open read-only.
- Alpha is Jensen's alpha (CAPM excess-return regression); raw-return alpha is never shown.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.
Core law: amber #E8A33D marks the user's own book — nothing else, ever.
