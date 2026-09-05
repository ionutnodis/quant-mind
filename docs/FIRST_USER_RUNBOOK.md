# First-user runbook

This runbook prepares one private, read-only IBKR portfolio for QuantMind. The source repository may be public; the portfolio cache is not. Use a paper session for the first acceptance pass. Do not expose QuantMind beyond the local machine and do not reuse a data directory between users.

## 1. Prepare the machine

Install Python 3.12+, [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh/), and either IB Gateway or Trader Workstation. From the repository root:

```bash
cp .env.example .env
uv sync --locked --dev
cd web
bun install --frozen-lockfile
bun run build
cd ..
```

Keep `.env`, `data/`, account identifiers, and portfolio screenshots private.

## 2. Select one IBKR account

Edit `.env` and set `QM_ACCOUNT_ID` to the single account this workspace may analyse. This is mandatory when the login can see multiple advisor, family-office, or subaccounts. QuantMind refuses to blend multiple visible accounts and uses an account-scoped portfolio stream rather than IBKR's global positions request, so the selected-account path remains viable for advisor/master sessions with more than 50 subaccounts.

```dotenv
QM_ACCOUNT_ID=YOUR_ACCOUNT_ID
QM_HOST=127.0.0.1
QM_PORT=4002
QM_CLIENT_ID=17
QM_DATA_DIR=data
QM_BENCHMARK=SPY
QM_BASE_CURRENCY=USD
# Optional: enrich checksum-valid ETF ISINs from a 30-day justETF cache.
QM_UCITS_METADATA_ENABLED=false
```

Use a unique client ID if another API program already uses `17` or `18`.

## 3. Configure IBKR read-only access

Log in to IB Gateway or TWS before starting QuantMind. Keep the IBKR API in read-only mode. Match `QM_PORT` to the socket configured in IBKR:

| Client | Paper | Live |
| --- | ---: | ---: |
| IB Gateway | `4002` | `4001` |
| TWS | `7497` | `7496` |

For TWS, enable socket clients under API settings. Recent IB Gateway versions enable socket access automatically. Keep connections local to `127.0.0.1`. Interactive Brokers documents these settings and port defaults in its [official API configuration guide](https://ibkrcampus.com/campus/trading-lessons/installing-configuring-tws-for-the-api/).

## 4. Start and finish Setup

From the repository root:

```bash
uv run python -m quantmind.api.main
```

Run only one QuantMind sync against a given `QM_DATA_DIR` at a time. The web action, the full CLI, and the option-chain CLI share a cross-process writer lock and reject an overlapping run instead of risking a mixed cache generation.

Before syncing, resolve any dual-listed holdings that IBKR reports under the same ticker to one canonical listing/symbol. The current `symbol_map` (`$QM_DATA_DIR/symbols.json`) can store only one canonical `conId` per display symbol, so QuantMind blocks an ambiguous book instead of merging two contracts.

Open `http://127.0.0.1:8000/book/setup` (`/setup` is retained as a first-use alias) and follow the single next action shown:

1. Confirm the local API is ready and IBKR is connected to the intended paper/live mode.
2. Select **Sync market data**. The job loads the starter risk universe, all four required macro series, held stock/option underliers, and any required ECB reference rates. The bounded option surface is augmented with every exact held contract, including weeklies, LEAPS, and far strikes. If FX remains missing, Setup offers a dedicated retry. A partial result stays visibly amber; warnings do not erase daily bars already written.
3. Select **Pin current book**. This writes an immutable, local `book_ref` for the selected account.
4. Open Portfolio from Setup and spot-check positions against IBKR.

If IBKR was not running when QuantMind started, start IBKR and restart QuantMind. If Setup says **Select one IBKR account**, set `QM_ACCOUNT_ID` and restart.

## 5. Acceptance checks

Do not rely on the installation until all applicable checks pass:

- Setup reports `READY`, with the expected paper/live mode.
- Portfolio position count, symbols, quantities, multipliers, and signs match IBKR.
- IBKR contract IDs, exchanges, currencies, and option terms match the intended listings. Held stocks are synced by their authoritative contract ID, and option-only holdings resolve their underlier through IBKR's `underConId`; neither path falls back to an ambiguous ticker re-resolution after authoritative lookup fails.
- Every display symbol resolves to one canonical listing and `conId`; no two listings share the same ticker in the acceptance book.
- No second account is present in the book.
- Market data has a recent as-of date and the expected benchmark.
- All required symbols and macro series are present; readiness is based on the oldest required observation, not the freshest file.
- Held options show strike, expiry, right, and multiplier after pinning.
- The held-option readiness card shows every contract priced from a fresh exact quote. The sleeve reports complete/partial/unavailable coverage and never substitutes the underlier price.
- Portfolio totals and weights appear only when every position has a usable mark. A priced subtotal or reported P&L is explicitly partial and must not be treated as a complete book value. Foreign-position unrealized P&L remains local-currency only because current FX is not acquisition-date cost evidence.
- The acceptance book contains only stocks, ETFs, and equity options. Split out futures, futures options, bonds, CFDs, FX/cash rows, and other unsupported security types.
- What-If and Hedge Lab explicitly refuse option/non-unit-multiplier books until their contract-aware repricing engines land; use them only for supported equity books in this alpha.
- Refreshing an analysis URL preserves the same `book_ref`.

## 6. Troubleshooting

- **Broker unavailable:** verify IBKR is logged in, the socket port matches `.env`, localhost connections are allowed, and client IDs are not already in use; then restart QuantMind.
- **Account selection required:** set one visible account in `QM_ACCOUNT_ID`; never work around this by aggregating subaccounts.
- **Cache empty/stale:** rerun Sync from Setup or `uv run python -m quantmind.sync_cli` and inspect the final warning/error.
- **Another sync is already writing:** let the active web or CLI sync finish, then retry. Do not delete `.quantmind-sync.lock`; the operating-system lease, not the file's presence, determines ownership and is released automatically if a process exits.
- **Pinned book stale or scoped to another account/mode:** use **Refresh current book**. QuantMind does not reuse yesterday's or another account's snapshot as the current book.
- **Multiple listings for the same ticker:** normalize the upstream holding to one canonical listing/symbol before sync. This alpha has no in-app alias or reconciliation layer for two `conId` values under one display symbol.
- **Instrument identity or metadata stale/mismatched:** do not edit `symbols.json` or `instruments.json`. Run Sync from Setup or `uv run python -m quantmind.sync_cli`; after a successful rebuild, pin a new book if QuantMind reports that the previous `book_ref` has changed identity.
- **Reporting currency changed:** sync the newly required FX evidence, then `POST /api/book/{old_book_ref}/rebase`. QuantMind creates a new immutable snapshot with the original positions and valuation time plus `rebased_from` lineage; it never rewrites the old book.
- **Missing FX/instrument identity:** set `QM_BASE_CURRENCY` to the reporting currency, refresh IBKR metadata, and run sync again. QuantMind uses dated ECB reference rates for stocks and ETFs, but blocks unknown currencies, missing/stale FX, cross-currency aggregate option Greeks, and unsupported security types.
- **Alpha unavailable in a non-USD book:** this is intentional in v0.5. `US3M` is a USD risk-free series, so QuantMind withholds Jensen alpha when `QM_BASE_CURRENCY` is not USD rather than mixing cash-rate regimes.
- **Option chain missing, stale, or bound to an old underlier identity:** run the market sync first if the underlier identity changed, then `uv run python -m quantmind.options_sync_cli NVDA MU` with the required market-data permissions. Replace symbols with held underliers.
- **Frontend unavailable at port 8000:** rebuild with `cd web && bun run build`; for a non-standard deployment set `QM_WEB_DIST` to the absolute build directory. For development, run `bun run dev` and use port 5173.

## Current pre-1.0 boundary

This release supports one local IBKR portfolio and deterministic synthetic demos. Stocks and ETFs can be normalized into `QM_BASE_CURRENCY` with dated ECB evidence; mixed-currency options are detected and aggregate monetary Greeks are withheld until legwise normalization is implemented. Unknown currencies and unsupported security types are blocked rather than coerced. Phones are a read-only companion; setup and analysis mutations require a viewport of at least 768 × 600. Moomoo import, multi-account switching, remote hosting, user authentication, order execution, and a public SaaS deployment are not enabled. Treat risk numbers as research outputs and independently verify them before decisions.
