# TODOS

## Unified options-aware ES (M3)
- **What:** Replace the two-view M1 risk report (returns-based ES + separate options stress grid) with one total-book distribution: simulate joint spot/vol paths and reprice option legs on each path.
- **Why:** M1's two views are honest but not additive; a single distribution is the only true "portfolio ES" for a book with an options overlay.
- **Pros:** ES/Monte Carlo numbers become decision-grade for the whole book, not just the equity sleeve.
- **Cons:** Needs vol-surface dynamics assumptions and meaningful compute; easy to build wrong — validate against the stress grid first.
- **Context:** Design doc "Outside-voice hardening" item 13 (Codex finding #4). Prereq: M1 stress grid and Monte Carlo working and trusted.
- **Depends on:** M1.5 deterministic whole-book scenario and local American/dividend-aware
  pricer validated; sufficient rights-cleared volatility-factor history.

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

## Black-Litterman / MVO allocation lens (eng-review deferred, 2026-07-26)
- **What:** Reverse-optimized equilibrium weights vs current weights, with optional view injection (Black-Litterman); mean-variance efficient frontier + QP as a later add.
- **Why:** A "does my tilt agree with equilibrium + my views" sanity check. Deferred from the dashboard-expansion program (scope B): heaviest subsystem for the lightest stated purpose (a lens, not an execution target) for a discretionary options book.
- **Pros:** Adds an allocation-sanity lens; the quant-flex piece.
- **Cons:** Needs a covariance estimator (Ledoit-Wolf), a market proxy (Roll's critique), and a QP dep; decorative unless you actually rebalance toward weights.
- **Context:** Design doc `nodisionut-expand-dashboard-from-video-design-*.md` (D1, NOT-in-scope). Sibling video #100.
- **Depends on:** analytics core (returns/covariance) shipped; a real desire for an allocation lens.
