# TODOS

## Unified options-aware ES (v2)
- **What:** Replace the two-view M1 risk report (returns-based ES + separate options stress grid) with one total-book distribution: simulate joint spot/vol paths and reprice option legs on each path.
- **Why:** M1's two views are honest but not additive; a single distribution is the only true "portfolio ES" for a book with an options overlay.
- **Pros:** ES/Monte Carlo numbers become decision-grade for the whole book, not just the equity sleeve.
- **Cons:** Needs vol-surface dynamics assumptions and meaningful compute; easy to build wrong — validate against the stress grid first.
- **Context:** Design doc "Outside-voice hardening" item 13 (Codex finding #4). Prereq: M1 stress grid and Monte Carlo working and trusted.
- **Depends on:** M1 risk core complete; QuantLib repricing validated.

## Liquidity/borrow/tax-aware hedge costs
- **What:** Extend v1 hedge sizing (integer contracts, spread-aware cost, IBKR what-if margin) with open interest/liquidity screens, borrow costs for shorts, and tax-lot awareness.
- **Why:** Mid-price rankings can reverse under real trading frictions (Codex finding #6).
- **Pros:** Hedge recommendations become executable as-shown.
- **Cons:** Data for borrow/liquidity is patchy at retail; diminishing returns below a portfolio size threshold.
- **Context:** v1 ships the tradeability basics; this is the completion pass.
- **Depends on:** v1 hedge ranking/sizing shipped and used for a few weeks.

## ibind / Web API migration evaluation
- **What:** Re-evaluate migrating the broker implementation from ib_async/IB Gateway to ibind + headless OAuth once (a) Gateway babysitting proves costly in practice, or (b) ibind's OAuth stack matures past beta.
- **Why:** Removes the Java Gateway process entirely; aligns with IBKR's unified OAuth 2.0 direction.
- **Pros:** Zero local processes to keep alive; simpler unattended overnight story.
- **Cons:** Beta library, stale-crypto OAuth dependency, weaker options-chain ergonomics today.
- **Context:** Design doc Approach C, kept open behind the broker interface (Engineering Constraint 20 bounds the surface that must be ported).
- **Depends on:** Broker interface stable; a concrete pain trigger.

## IBIS Research Essentials decision
- **What:** Run the one-month free IBIS trial after the Gateway spike; diff `reqNewsProviders` and calendar/fundamentals API access before vs during trial; buy ($69/mo), buy à la carte, or skip.
- **Why:** Closes the news gap and adds the earnings/economic calendar (vol events matter for an options book) — but only if the feeds are API-accessible, which is unverified.
- **Pros:** Dow Jones/Reuters/Briefing/Fly headlines + earnings calendar would complete v1's news tier in one purchase.
- **Cons:** Research-platform content may be UI-only; $828/yr if bought blind.
- **Context:** Design doc Data Entitlement Audit, IBIS bullet. The "short gamma into earnings" risk flag depends on calendar access.
- **Depends on:** Gateway spike running (needed to test API feed visibility).

## IV history bootstrap
- **What:** Decide between accumulating implied-vol history locally (IV rank matures after N months) vs a one-time historical vol data purchase.
- **Why:** IV rank/skew (v1 feature) needs ~1 year of IV history that neither IBKR nor a fresh cache provides at install.
- **Pros:** Accumulation is free; purchase makes IV rank meaningful on day one.
- **Cons:** Accumulation delays the feature's usefulness; purchase adds a vendor and normalization work (single-provenance rule applies).
- **Context:** Design doc Open Question 9.
- **Depends on:** Options chain ingestion working (to know exactly which IV series to backfill).

## In-app sync action (design-review deferred, 2026-07-25)
- **What:** POST /api/sync endpoint (job-manager backed, subprocess or in-process broker) + a sync button in the Today staleness banner and empty state.
- **Why:** The UI currently tells the user to run a terminal command — poor workbench utility language (design-review FINDING-005).
- **Depends on:** JobManager (exists); decide subprocess vs in-process broker connect.

## Responsive layout pass (design-review deferred, 2026-07-25)
- **What:** Breakpoint behavior for sidebar, tile strip, and panels below ~1024px.
- **Why:** Desktop-first is deliberate (personal tool, external monitor), but a laptop-width squeeze currently gets no accommodations.

## Deferred from Batch-1 final review
- **BookBuilder pinned-state chip** — show which pinned `book_ref` a builder is editing (Batch 2).
- **BookBuilder option-leg inputs** — strike/expiry/right rows in the shared builder (Batch 2 What-If spec).
- **Cosmetics** — dead `rng` fixture and aligned-sample comment cleanups flagged in review.
- **Drift-artifact commit-ordering process note (F9)** — regenerate `openapi.json` + `web/src/lib/api-types.ts` in the SAME commit as the schema change, per-commit not per-batch, so no intermediate commit fails the drift gate.
- **Radix vs plain-div popovers** — InstrumentHover/Sheet are plain positioned divs today; adopting Radix primitives (DESIGN.md component base) is pending user decision.
- **NewsTicker relevance-filter tuning** — keyword/symbol filter precision beyond the new "showing latest broadtape" fallback.

## Pre-wave-3 consolidation pass (final-review mandated, 2026-07-25)
- **What:** Extract shared `api/routers/_shared.py` (`_clean`, `_iso`, `_read_close_series`, the qty-nonzero PositionIn model — currently 7/4/2/2 duplicated copies); shared `BookBuilder` React component (Hedge's string-qty variant as base — WhatIf and Hedge have diverging row-builders); align Hedge's degenerate-input 422s (gross<=0, non-finite closes) with WhatIf's named-422 policy; narrow the Engle-Granger broad except.
- **Why:** Wave-2's exclusive-file-ownership rules tripled helper duplication (right tradeoff then, wrong to keep); any NaN-policy change now needs 7 edits; a third row-builder copy in wave 3 locks in drift.
- **Context:** Wave-2 final whole-branch review M1/M2/M5; ledger history in git (docs/plans/2026-07-25-wave2.md).
