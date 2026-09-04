# Changelog

All notable changes to QuantMind are documented in this file.

## [0.5.0.0] - 2026-09-04

### Added

- Dated ECB FX ingestion and base-currency normalization for European stock/ETF portfolio values, What-If, Hedge, Risk, Monte Carlo, and factor regression paths.
- Opt-in, ISIN-addressed UCITS ETF profile enrichment with a typed 30-day cache, provenance links, freshness states, and responsive instrument-sheet display.
- Public-repository data-source and security boundaries, stronger local-secret/data exclusions, and a plain-language operator guide with current-product screenshots and clearly labeled roadmap mockups.

### Changed

- Portfolio and account summaries now preserve local-currency values, add explicit reporting-currency fields, and withhold totals or P&L that cannot be supported by dated conversion evidence.
- Risk, instrument, What-If, Hedge, leverage, and historical-return paths now use the same reporting-currency normalization and expose the FX evidence behind the result.
- Data sync now isolates symbol and source failures, publishes independent successful phases, bounds external downloads, and treats research/context instruments as optional rather than blocking a held book.
- yfinance fallback now requires a validated quote currency and preserves the original quote convention; London pence/GBX bars are normalized to GBP before storage.
- Setup now diagnoses required FX, UCITS profile freshness, reporting-currency changes, missing contract identity, and cross-currency option limitations with one explicit next action.

### Fixed

- FX refreshes publish immutable generation-addressed series before atomically switching the manifest, so an interrupted or same-second refresh cannot expose a mixed generation.
- Foreign account summaries without a usable conversion rate now remain readable in their broker currency with a named warning instead of failing the full Portfolio response.
- Historical portfolio marks and underlier exposures now use FX evidence from each mark's own observation date, preventing newer reference rates from leaking into older prices.
- Option-only holdings resolve the exact IBKR underlier contract, justETF redirects are allowlisted before response bodies are read, and default hedge discovery fills its eligibility budget before truncating results.
- What-If and Hedge now derive displayed marks, weights, valuation, and `as_of` from the same aligned observation; long/short hedge sizing uses gross exposure and candidate protection is compared on one common ES window.
- Stale underlier marks can no longer produce portfolio exposure, option Greeks, or stress P&L, and foreign benchmark FX is loaded independently from held-position currencies.
- Failed live-book discovery now remains a blocking cache requirement, same-ticker multi-listing conflicts fail closed, malformed instrument masters produce named evidence errors, and all documented sync entry points share a datastore-wide process lock.
- Missing, corrupt, stale, malformed, path-traversing, and legacy FX/UCITS evidence now follows tested fail-closed or explicit-degraded behavior.
- Analytical responses retain FX provenance, requested hedge candidates can no longer disappear silently, and older pinned books can be rebased into a new reporting-currency snapshot without rewriting history.
- Non-USD risk views no longer subtract the USD `US3M` series when calculating alpha.

## [0.4.0.0] - 2026-09-04

### Added

- A guided first-run workbench that checks broker connectivity, required market and macro evidence, immutable-book freshness, currency support, and exact held-option coverage before declaring the portfolio ready.
- Browser actions to sync evidence, pin the selected IBKR book, and carry the resulting reference into portfolio analysis without enabling order placement.
- Production-safe deep links plus responsive authoring layouts for laptops, tablets, and ultrawide displays, with a read-only companion experience on phones.
- A first-user runbook, environment template, and acceptance checklist for configuring and validating a paper IBKR session.

### Changed

- IBKR sessions now select exactly one configured account and fail closed when multiple accounts are visible without `QM_ACCOUNT_ID`; pinned books retain a private account fingerprint and broker mode for scope checks.
- Market and macro readiness is based on the weakest required observation, while lightweight cache watermarks keep continuous setup status checks inexpensive.
- Live option contracts retain strike, expiry, right, multiplier, currency, and exchange identity; the default sync augments its bounded surface with every exact held contract, including weeklies, LEAPS, and far strikes.
- Portfolio option values now use exact cached bid/ask marks and report complete, partial, stale, or unavailable coverage instead of substituting the underlying price.
- Selected advisor accounts now use IBKR's account-scoped portfolio stream, and held stocks retain their authoritative contract IDs during market-data sync.

### Fixed

- Broker disconnects now invalidate the live session immediately, and completed or partial sync outcomes remain visible in the setup workflow.
- Stale or cross-account book snapshots, unsupported non-USD positions, and corrupt local evidence fail closed or degrade to explicit unavailable states instead of producing misleading analytics.
- Incomplete position marks no longer produce apparently complete totals or normalized weights; unsupported security types, non-USD account totals, ambiguous option contracts, and modified snapshot contents are rejected explicitly.
- Single-page routes such as `/book/setup` and `/portfolio` now survive direct navigation in the production server while unknown API paths retain normal 404 behavior.

## [0.3.0.0] - 2026-08-23

### Added

- A local-first FastAPI and React risk workbench with portfolio, factor-risk,
  hedge, scenario, macro, research-lab, options, news, and data-sync surfaces.
- Canonical book and risk contracts, immutable analytical snapshots, provenance
  manifests, SQLite publication history, active-pointer recovery, and corruption
  detection for reproducible one-book analysis.
- Read-only IBKR integration seams, options-chain and news adapters, deterministic
  synthetic fixtures, generated OpenAPI types, and hermetic browser smoke tests.

### Changed

- Consolidated shared book-leg handling so What If and Hedge can explicitly pin a
  live book and reuse its immutable reference across calculations.
- Aligned package and API metadata on the four-part pre-1.0 release version.

### Fixed

- Hardened publication verification, durable result attestation, collaborator
  isolation, catalog causality, and CI setup resolution.
- Rejected non-finite position quantities and option terms before persistence, and
  made malformed request-validation evidence safe to serialize as a 422 response.
