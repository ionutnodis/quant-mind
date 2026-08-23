# Changelog

All notable changes to QuantMind are documented in this file.

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
