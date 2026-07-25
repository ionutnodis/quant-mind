# QuantMind

Personal quant laboratory: Python core (`src/quantmind/`) + FastAPI backend (planned) + React frontend (planned), IBKR via ib_async behind a broker interface, DuckDB/Parquet cache. Design doc: `~/.gstack/projects/ionutnodis-quant-mind/nodisionut-ibkr-quant-dashboard-design-design-20260725-111258.md`.

## Testing
- Framework: pytest (`uv run pytest`). E2E paper-Gateway tests are opt-in: `uv run pytest -m e2e --override-ini addopts=''` (needs IB Gateway on port 4002).
- TDD is the law here: no production code without a failing test first. Risk-math modules test against hand-computed/golden values.

## Engineering constraints (full list in the design doc)
- Pure core: `risk/`, `analytics/`, `hedge/` are pure picklable functions; I/O only in `broker/`, `sources/`, `datastore/`.
- Risk math uses ADJUSTED_LAST bars keyed by conId; adjusted history is refreshable, never append-only.
- Parquet is the source of truth; exactly one writer process; DuckDB readers open read-only.
- Alpha is Jensen's alpha (CAPM excess-return regression); raw-return alpha is never shown.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.
Core law: amber #E8A33D marks the user's own book — nothing else, ever.
