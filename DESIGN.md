# Design System — QuantMind

## Audience Risk Product Addendum — Approved 2026-08-20

This addendum is authoritative for the Book Risk X-Ray audience product. It supersedes the
historical document's **Product Context**, seven-page **Information Architecture**, C-owned
sidebar/layout decision, claim that shadcn/Radix is the installed component base, and the
desktop-only responsive decision. Typography, color, spacing, density, chart discipline,
motion restraint, and the “amber marks the user's book” law remain in force unless amended
below.

- **Product:** one-book risk truth and decision engine for the founder plus an experienced
  private design partner, with a path to invite-only use. It remains local/read-only for the
  alpha and does not submit orders or recommend trades.
- **Approved visual direction — `REMIX_BAC`:** B owns the analytical shell, sectioned
  navigation, trust cluster, anchor-first X-Ray, and density; A owns the deterministic
  editorial thesis, ruled evidence line, and memo-consistent voice; C owns state-first
  reconciliation and staged delta-first Decide review.
- **Visual-intent artifact:** `~/.gstack/projects/ionutnodis-quant-mind/designs/risk-product-redesign-20260820/approved-remix.html`
  plus its four `final-*.png` screens are authoritative for hierarchy, tokens, composition,
  and state semantics only. Its fixed 1440px board, scaled 7px-minimum type, and separate
  mobile DOM are not implementation patterns. `design-board.html` remains exploratory.
- **Sole home:** Risk X-Ray is introduced at `/xray` and becomes `/` only after snapshot,
  responsive, accessibility, and parity gates pass. There is no parallel Today page after
  cutover. Former Today + Macro capability lives in Markets.
- **Primary navigation:** Risk X-Ray, Book, Factors, Scenarios when promoted, Decide.
- **Secondary navigation:** Markets, Research Lab, Data & Models. Book owns the contextual
  `/book/setup` Setup Run; `/setup` is only a first-use alias.
- **First-composition constraint:** trust/snapshot state → deterministic thesis and ruled
  evidence line → paired capital-versus-risk anchor. Equal KPI cards do not replace the
  evidence line.
- **Shell:** B-style slim sectioned sidebar and persistent trust-cluster top bar. C's question
  outline and checklist are component patterns for evidence/recovery, not global navigation.
- **Status lexicon:** healthy `●` + word in green; caution `▲` + word in warning yellow;
  failed/breached `×` + word in red; refused/informational `◇` + word in steel. Color never
  carries state alone, and book amber is not a status color.
- **Actions:** primary actions use neutral/steel interface chrome. Amber remains reserved for
  live user-book data plus the established wordmark and active-navigation exceptions.
- **Current component baseline:** React 19, TanStack Router/Query, Tailwind CSS 4, `cmdk`, the
  existing Shell/Panel vocabulary, and themed Plotly. shadcn/Radix is not an installed system
  baseline; introduce accessible primitives selectively and deliberately rather than hiding a
  wholesale dependency migration inside the redesign.
- **Signature visual:** capital-versus-risk is a bespoke accessible DOM/SVG component with a
  real synchronized table, keyboard row navigation, pinned focus, signed zero axes, and shared
  entity ordering. Plotly remains appropriate for conventional factor/scenario charts, not
  for this connector-line interaction.
- **Responsive policy:** one semantic component tree reflows continuously from 320 through
  3440 CSS px. Authoring requires at least 768px width and 600px height; otherwise QuantMind
  becomes a deliberate read-only companion for snapshot truth, thesis, top drivers,
  scenario/limit state, positions, saved evaluations, and responsive memo viewing. This keeps
  capable iPads in staged authoring while short landscape phones remain companion-only.
- **Responsive mechanics:** media queries own global product mode; named container queries own
  local panel reflow. Shared primitives use `minmax()`, `auto-fit`, `clamp()`, logical
  properties, user-respecting `rem`, and one `ResponsivePlot`/`ResizeObserver` adapter. Pointer
  coarseness enlarges every target to at least 44×44px, including on authoring iPads; it does
  not revoke authoring. The document never scrolls horizontally.
- **Wide-screen policy:** there is no global app-width cap at 2560–3440px. The analytical
  canvas adds evidence/diagnostic columns and comparison context while prose (65–72ch), table
  columns, and chart heights retain local bounds. A single chart or paragraph never stretches
  merely because space exists.

## Historical Product Context (superseded where the addendum differs)
- **What this is:** A personal quant laboratory — "what a garage is to a mechanic." Seven-page workbench over a Python risk engine: portfolio truth, risk analytics, hedge decisions, what-if sandboxing, macro context, and a model Lab (fit → diagnose → simulate → apply to book).
- **Who it's for:** Exactly one user — a quant-literate investor (beta core + options overlay at IBKR) who already uses TradingView and Koyfin. QuantMind **augments** those tools; it never rebuilds charting/screening. Its monopoly: it knows his book.
- **Space/industry:** Personal quantitative finance. Peers (for literacy, not imitation): TradingView, Koyfin, OpenBB — all converge on interchangeable blue-accent SaaS-dark; QuantMind deliberately does not.
- **Project type:** Multi-page React web app (FastAPI/Python backend), local-first, dark-first.
- **North star:** "The terminal Bloomberg never built for retail" — meaning *capability and seriousness*, explicitly NOT retro-terminal aesthetics (rejected: amber-on-black mono cosplay).

## Aesthetic Direction
- **Direction:** Modern professional workbench — Linear/Mercury-grade craft applied to money. Industrial precision in contemporary language.
- **Decoration level:** Minimal — hairline borders and subtle elevation do the separating; the data is the decoration. No gradients, no glows, no decorative illustration.
- **Mood:** Calm, mathematically serious, personal. A laboratory bench, not a dashboard; a tool that augments, not a demo that impresses.
- **Approved direction artifacts:** `~/.gstack/projects/ionutnodis-quant-mind/designs/design-system-20260725/` (approved.json = remix: C's structure incl. saved-models console + B's chart discipline + B's wordmark/controls + C's diagnostic depth).

## Historical Information Architecture (superseded by the addendum)
1. **Today** — morning entry. Regime summary, book vitals, overnight moves ranked by impact on the book, calendar flags. Glanceable ≤60s.
2. **Portfolio** — the truth about the book. Dense sortable positions (equity core vs options overlay), aggregate Greeks, exposure breakdowns, P&L attribution.
3. **Risk** — rolling beta/alpha, ES with history, Monte Carlo fans, spot×vol stress grids, drawdowns. Parameters adjustable.
4. **Hedge Lab** — objective picker (cut beta to X / floor loss / cap vega) → ranked, sized, costed candidates with margin impact; correlation/cointegration as diagnostics.
5. **What-If** — clone the book, modify, watch risk recompute side-by-side vs live. Saved scenarios.
6. **Macro** — yields/curve, liquidity, factors, rotation — each with the user's exposure noted. Links OUT to TradingView for charting (augmentation posture made literal).
7. **Lab** — the centerpiece. Models are first-class objects from a Python registry (OU/Vasicek/CIR, GARCH, EVT tails, copulas — extensible). Every model gets the same bench: data picker → fit → **diagnostics with full mathematical transparency** (parameter estimates with confidence intervals, log-likelihood/AIC, residual and QQ plots) → simulate (fan chart) → **Apply to Book** (simulation piped into the portfolio risk engine → P&L distribution, ES, exposure deltas). Bottom console strip: saved models with status chips and last-run timestamps.

## Typography
- **Display/Wordmark:** General Sans (700/600) — the approved variant-B wordmark treatment; modern grotesk authority without Inter-sameness.
- **Body & UI labels:** General Sans (400/500) — one voice; loaded via Fontshare.
- **Data/Numerics (all numbers, everywhere):** Geist Mono with `font-variant-numeric: tabular-nums` — contemporary mono, aligned columns, no cockpit LARP. Greek letters (θ, μ, σ) set in the mono for model parameters.
- **Code (Lab expressions/configs):** Geist Mono.
- **Loading:** Fontshare CDN for General Sans; Geist Mono self-hosted or via Vercel CDN.
- **Scale:** 12 / 13 (data/metadata only) / 14 / 16 (body and mobile minimum) / 20 / 26 / 32px. Dense desktop tables may run at 12–13px with zoom support; page titles run at 20px and the X-Ray thesis at 20–26px. Line-height 1.3 data, 1.5 prose.

## Color
- **Approach:** Restrained + one semantic law: **amber marks the user's book — nothing else, ever.**
- **Ground:** `#0A0A0B`; **Surface/elevated:** `#131316` → `#1A1A1E` (two elevation steps max); **Hairline:** `#26262B`
- **Text:** primary `#EDEDEF`, muted `#8B8B93`
- **You/Book (THE accent):** `#E8A33D` — user's positions, P&L attribution, "your book" chart lines, Apply-to-Book results, active nav state. Never used decoratively, never on market data.
- **Market/neutral data viz:** steel `#7FA0B4` and neutral grays — simulation fans, benchmark lines (approved variant-B discipline).
- **Semantic:** up `#2FBF71` / down `#E5484D` (conventional — the user lives in TradingView; fight no learned conventions), warning `#D9A63F` (distinct from book-amber by context: only in badges/banners), info `#7FA0B4`.
- **Dark mode:** dark IS the design. Light mode: full re-derivation (paper `#FAFAF8`, ink `#1A1A1C`, same amber law), not an inversion; ship after dark is settled.

## Components
- **Base:** React 19 + TanStack Router/Query + Tailwind CSS 4 + `cmdk`, evolving the existing Shell/Panel/table vocabulary. Radix/shadcn is not the installed baseline; add accessible primitives selectively when a focus-managed dialog, sheet, popover, or tooltip requires them.
- **⌘K command palette** is the navigation spine ("go x-ray", "open factors", "test a change", "fit ou on 10y"). Every milestone-available route is reachable and every Lab action runnable from it; unpromoted capabilities are absent.
- **Every data panel carries an as-of stamp;** staleness is flagged visibly, never hidden. `NO MATERIAL LINK` is stated honestly when portfolio linkage is immaterial.
- **Charts:** Plotly (dark template themed to this palette). Fan charts: percentile bands in steel with opacity ramp, median line solid; the book's line is always amber; no gridline clutter (hairline gridlines `#26262B` only).

## Spacing
- **Base unit:** 4px. **Density:** compact-professional — Mercury-bank density in data zones, normal breathing room around page titles and controls.
- **Scale:** 2xs(2) xs(4) sm(8) md(12) lg(16) xl(24) 2xl(32) 3xl(48)

## Layout
- **Approach:** grid-disciplined B-derived app shell. Persistent slim sectioned sidebar on desktop (wordmark top, primary risk workflow, secondary context, user/footer bottom) and a trust-cluster top bar that separates active-snapshot truth from latest-run outcome. C-style outlines/checklists are local state components, not the global shell.
- **Grid:** 12-col content area; Lab uses the three-zone bench (Model | Simulate | Apply-to-Book) + bottom saved-models console.
- **Max content width:** none (fluid, workbench uses the screen); comfortable gutters 24px.
- **Border radius:** sm 4px (inputs, chips), md 6px (panels, buttons). Nothing bubblier.

## Motion
- **Approach:** minimal-functional. Value updates tick (~120ms), panel/sheet transitions 150–200ms ease-out, recompute states use skeleton shimmer only in the affected panel. No entrance choreography, no scroll effects.
- **Easing:** enter ease-out, exit ease-in, move ease-in-out. **Duration:** micro 100ms, short 150–200ms, medium 250ms. Nothing longer.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-25 | Initial system via /design-consultation (research + Codex & Claude outside voices + 2 rounds of AI variant boards) | User rejected Bloomberg-retro "Instrument Grade" v1 as costume; repositioned as modern workbench that augments TradingView/Koyfin |
| 2026-07-25 | Amber `#E8A33D` reserved exclusively for user-book data | The "personal" differentiator made visual; survived both design rounds |
| 2026-07-25 | Conventional green/red up/down restored | User lives in TradingView; direction-by-glyph experiment dropped |
| 2026-07-25 | Approved remix: C structure (incl. saved-models console) + B chart discipline + B wordmark/controls + C diagnostic depth | Board feedback round 2 (ratings B5/A4/C4) |
| 2026-07-25 | Lab = model registry with fit→diagnose→simulate→apply-to-book pipeline as product centerpiece | "Garage for a mechanic" — flexibility + mathematical precision, tied back to the real portfolio |
| 2026-07-25 | Sanctioned amber uses: wordmark accent + active-nav state ("you are here"); `--color-warning #D9A63F` added for caution banners | /design-review — every other amber is book data |
| 2026-07-25 | Desktop-first: no mobile breakpoints in v1 (personal tool, external monitor); revisit on demand | Historical decision, superseded by D3 on 2026-08-20 |
| 2026-08-20 | Audience product direction `REMIX_BAC`: B analytical shell + A editorial thesis + C state-first reconciliation/Decide review | Founder approval after Kimi K3 co-design and A/B/C comparison board |
| 2026-08-20 | Risk X-Ray becomes the sole home; Today + Macro fold into Markets; old flat routes become migration redirects/components | Removes two competing home screens and preserves one source of risk truth |
| 2026-08-20 | Book owns the contextual Setup Run at `/book/setup`; Risk X-Ray summarizes, Data & Models owns source/model evidence | D2 founder decision; one recovery path without cloning controls across routes |
| 2026-08-20 | Below 768px wide or 600px high ships as a read-only companion; capable iPads retain staged authoring | D3 refined by engineering review; avoids the landscape-phone authoring hole without device detection |
| 2026-08-20 | `REMIX_BAC` is authoritative for visual intent, not its fixed scaling/separate mobile implementation | Kimi K3, GPT-5.6-SOL, and Claude Opus 5 converged on one semantic tree and continuous reflow |
