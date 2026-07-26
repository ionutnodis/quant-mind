/**
 * Hedge Lab page tests (Task 3, wave-2 plan; book builder swap + book_ref,
 * wave-3 Task A1): objective + book builder -> Run -> ranked candidates
 * table. Protection is the ranking key (amber, per the wave-2 Global
 * Constraints addendum: hypothetical/hedge results ARE the user's book for
 * color purposes); corr stability is a labeled diagnostic column only,
 * never the rank (cointegration was removed from this response/page —
 * pre-wave-3 consolidation pass, TODOS.md; its home is Lab's pair pipeline).
 * No @testing-library/user-event dependency in this repo — fireEvent only
 * (pattern: src/test/lab.test.tsx).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { Hedge } from "../pages/Hedge";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  // The book_ref pre-load tests plant a ?book_ref= URL param; scrub it so
  // later renders don't try to pre-load a book no handler serves.
  window.history.replaceState(null, "", "/");
});
afterAll(() => server.close());

function renderHedge() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Hedge />
    </QueryClientProvider>
  );
}

// es_before/es_after/protection are FRACTIONS of gross (historical_es on
// daily returns), never dollars — realistic magnitudes are ~0.001-0.05.
// Regression guard for C1: a dollar-scaled fixture (e.g. 250.0) would let a
// num(x, 0) render bug ("0 -> 0 (0)") slip through unnoticed, so these are
// pinned to real fraction values and the rendered percent strings are
// asserted below.
const HEDGE_RESPONSE = {
  benchmark: "SPY",
  objective: { kind: "beta_target", value: 0.0 },
  book_value: 10000.0,
  book_beta: 1.0,
  es_before: 0.0231,
  bench_expected_return_annual: 0.07,
  n_candidates_evaluated: 3,
  as_of: "2026-07-24T00:00:00Z",
  option_underlier: "SPY",
  option_chain_as_of: "2026-07-24",
  option_note:
    "sized off the -20% stress-grid node of the SPY sleeve; overlay repriced at constant time-to-expiry and IV",
  option_hedges: [
    {
      kind: "protective_put",
      expiry: "20261218",
      expiry_years: 0.4025,
      legs: [{ action: "long", strike: 430.0, right: "P", price: 8.4 }],
      contracts: 22.6,
      net_premium_per_contract: 840.0,
      cost_annual: 0.046,
      es_before: 0.0231,
      es_after: 0.012,
      protection: 0.0111,
      protection_per_cost: 0.241,
      delta_es_ci_low: 0.008,
      delta_es_ci_high: 0.015,
      tail_n_days: 25,
      tail_mean_book: -0.021,
      tail_mean_hedged: -0.005,
    },
    {
      kind: "collar",
      expiry: "20261218",
      expiry_years: 0.4025,
      legs: [
        { action: "long", strike: 430.0, right: "P", price: 8.4 },
        { action: "short", strike: 470.0, right: "C", price: 6.0 },
      ],
      contracts: 22.6,
      net_premium_per_contract: 240.0,
      cost_annual: 0.013,
      es_before: 0.0231,
      es_after: 0.014,
      protection: 0.0091,
      protection_per_cost: 0.7,
      delta_es_ci_low: 0.005,
      delta_es_ci_high: 0.013,
      tail_n_days: 25,
      tail_mean_book: -0.021,
      tail_mean_hedged: -0.009,
    },
  ],
  es_note: "ES = historical expected shortfall (97.5%) of DAILY returns over the 5y window, as a fraction of book gross",
  cost_note:
    "cost/yr = carry drag (β_h · E[r_bench]; E[r_bench] = +7.00%/yr from cached daily bars over the window) + borrow proxy 0.30%/yr on short/inverse notional (a labeled PROXY, not a quoted borrow rate)",
  ci_note: "ΔES interval = 95% CI from a seeded paired block bootstrap (block=5, n=500) of daily returns",
  tail_note: "tail panel = mean DAILY book return on the worst-decile SPY days in the window, with vs without each hedge",
  notes: [],
  candidates: [
    {
      symbol: "QQQ",
      beta: 0.82,
      unusable: false,
      hedge_qty: -21.3,
      hedge_notional: -8500.5,
      es_before: 0.0231,
      es_after: 0.009,
      protection: 0.0141,
      carry_drag_annual: 0.028,
      borrow_proxy_annual: 0.0015,
      cost_annual: 0.0295,
      protection_per_cost: 0.478,
      delta_es_ci_low: 0.011,
      delta_es_ci_high: 0.018,
      tail_n_days: 25,
      tail_mean_book: -0.021,
      tail_mean_hedged: -0.008,
      residual_beta: 0.05,
      corr_stability: 0.04,
    },
    {
      symbol: "IWM",
      beta: 0.55,
      unusable: false,
      hedge_qty: -30.1,
      hedge_notional: -6200.2,
      es_before: 0.0231,
      es_after: 0.015,
      protection: 0.0081,
      carry_drag_annual: 0.02,
      borrow_proxy_annual: 0.001,
      cost_annual: 0.021,
      protection_per_cost: 0.386,
      delta_es_ci_low: 0.004,
      delta_es_ci_high: 0.012,
      tail_n_days: 25,
      tail_mean_book: -0.021,
      tail_mean_hedged: -0.012,
      residual_beta: 0.1,
      corr_stability: 0.09,
    },
    {
      symbol: "FLAT",
      beta: 0.0,
      unusable: true,
      hedge_qty: null,
      hedge_notional: null,
      es_before: 0.0231,
      es_after: null,
      protection: null,
      carry_drag_annual: null,
      borrow_proxy_annual: null,
      cost_annual: null,
      protection_per_cost: null,
      delta_es_ci_low: null,
      delta_es_ci_high: null,
      tail_n_days: null,
      tail_mean_book: null,
      tail_mean_hedged: null,
      residual_beta: null,
      corr_stability: 0.5,
    },
  ],
};

test("shows an honest awaiting state before a run", async () => {
  renderHedge();
  expect(screen.getByRole("heading", { name: /objective/i })).toBeInTheDocument();
  expect(screen.getByText(/awaiting run/i)).toBeInTheDocument();
  // Book builder starts with one row so `book` (min 1) can always be submitted.
  expect(screen.getAllByLabelText(/symbol/i).length).toBeGreaterThan(0);
});

test("build a book, run, and render the ranked candidates table in amber", async () => {
  server.use(http.post("/api/hedge", () => HttpResponse.json(HEDGE_RESPONSE)));
  renderHedge();

  const symbolInputs = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInputs[0], { target: { value: "spy" } });
  const qtyInputs = screen.getAllByLabelText(/qty/i);
  fireEvent.change(qtyInputs[0], { target: { value: "10" } });

  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

  const table = await screen.findByTestId("candidates-table");
  const rows = within(table).getAllByRole("row");
  // header + 3 candidates
  expect(rows.length).toBe(4);

  // Ranked by protection desc: QQQ (160) before IWM (100) before FLAT (unusable, last).
  const bodyRows = rows.slice(1);
  expect(within(bodyRows[0]).getByText("QQQ")).toBeInTheDocument();
  expect(within(bodyRows[1]).getByText("IWM")).toBeInTheDocument();
  expect(within(bodyRows[2]).getByText("FLAT")).toBeInTheDocument();

  // Protection column is amber (the book-impact number).
  const protectionCell = within(bodyRows[0]).getByTestId("protection-cell");
  expect(protectionCell.className).toMatch(/text-you/);

  // C1 regression guard: es_before/es_after/protection are fractions of
  // gross, not dollars — they must render as percentages, not as "0 -> 0
  // (0)" (the num(x, 0) bug that hid every real candidate's protection).
  expect(protectionCell.textContent).toContain("2.31% → 0.90%(1.41%)");
  const iwmProtectionCell = within(bodyRows[1]).getByTestId("protection-cell");
  expect(iwmProtectionCell.textContent).toContain("2.31% → 1.50%(0.81%)");

  // Unusable candidate is flagged, not silently dropped.
  expect(within(bodyRows[2]).getByText(/unusable/i)).toBeInTheDocument();

  // Corr stability is labeled a diagnostic, never presented as the rank;
  // there is no cointegration column (removed, pre-wave-3 consolidation).
  expect(screen.getByText(/diagnostic/i)).toBeInTheDocument();
  expect(screen.queryByText(/coint/i)).not.toBeInTheDocument();
});

// --- wave-3B "Hedge honest": cost columns, ΔES CI, option hedges, tail panel ---

async function runWithFixture() {
  server.use(http.post("/api/hedge", () => HttpResponse.json(HEDGE_RESPONSE)));
  renderHedge();
  const [symbolInput] = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInput, { target: { value: "spy" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  return await screen.findByTestId("candidates-table");
}

test("cost column renders carry+borrow as %/yr and the rank is protection-per-cost", async () => {
  const table = await runWithFixture();
  const bodyRows = within(table).getAllByRole("row").slice(1);

  // QQQ: cost_annual 0.0295 -> "2.95%"; protection_per_cost 0.478 -> "0.48".
  const qqqCost = within(bodyRows[0]).getByTestId("cost-cell");
  expect(qqqCost.textContent).toContain("2.95%");
  expect(within(bodyRows[0]).getByTestId("ppc-cell").textContent).toContain("0.48");
  // Unusable candidate renders an honest dash, not zeros.
  expect(within(bodyRows[2]).getByTestId("cost-cell").textContent).toContain("—");

  // The methodology labels are on the page: proxy label + horizon labels.
  expect(screen.getByText(/labeled proxy/i)).toBeInTheDocument();
  expect(screen.getByText(/daily returns over the/i)).toBeInTheDocument();
});

test("delta-one option note from the backend is rendered, never silent", async () => {
  server.use(
    http.post("/api/hedge", () =>
      HttpResponse.json({
        ...HEDGE_RESPONSE,
        notes: [
          "Option legs are priced as delta-one underlier notional (qty x multiplier x spot) in this returns-based engine — a declared approximation; Greeks-aware option risk lives in the options layer (book-greeks).",
        ],
      })
    )
  );
  renderHedge();
  const [symbolInput] = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInput, { target: { value: "spy" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("candidates-table");
  expect(screen.getByText(/delta-one underlier notional/i)).toBeInTheDocument();
});

test("delta-ES bootstrap interval is displayed as an interval", async () => {
  const table = await runWithFixture();
  const bodyRows = within(table).getAllByRole("row").slice(1);
  // QQQ CI [0.011, 0.018] -> "[1.10%, 1.80%]".
  const ci = within(bodyRows[0]).getByTestId("ci-cell");
  expect(ci.textContent).toContain("[1.10%, 1.80%]");
  // The CI methodology (bootstrap) is stated.
  expect(screen.getByText(/block bootstrap/i)).toBeInTheDocument();
});

test("option hedge structures render with premium drag and legs", async () => {
  await runWithFixture();
  const table = await screen.findByTestId("option-hedges-table");
  const bodyRows = within(table).getAllByRole("row").slice(1);
  expect(bodyRows.length).toBe(2);
  expect(within(bodyRows[0]).getByText(/protective put/i)).toBeInTheDocument();
  expect(within(bodyRows[1]).getByText(/collar/i)).toBeInTheDocument();
  // Legs: long 430P / short 470C on the collar row.
  expect(within(bodyRows[1]).getByText(/long 430P/i)).toBeInTheDocument();
  expect(within(bodyRows[1]).getByText(/short 470C/i)).toBeInTheDocument();
  // Premium as annual drag: 0.046 -> "4.60%".
  expect(within(bodyRows[0]).getByTestId("option-cost-cell").textContent).toContain("4.60%");
  // Chain provenance is stamped.
  expect(screen.getByText(/chain as of 2026-07-24/i)).toBeInTheDocument();
});

test("missing chain degrades to the structured option note, never a crash", async () => {
  server.use(
    http.post("/api/hedge", () =>
      HttpResponse.json({
        ...HEDGE_RESPONSE,
        option_hedges: [],
        option_chain_as_of: null,
        option_note: "no cached option chain for SPY — run options_sync_cli to snapshot one",
      })
    )
  );
  renderHedge();
  const [symbolInput] = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInput, { target: { value: "spy" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("candidates-table");
  expect(screen.getByText(/no cached option chain for SPY/i)).toBeInTheDocument();
  expect(screen.queryByTestId("option-hedges-table")).not.toBeInTheDocument();
});

test("tail-conditional panel shows book P&L with vs without each hedge", async () => {
  await runWithFixture();
  const table = await screen.findByTestId("tail-table");
  const bodyRows = within(table).getAllByRole("row").slice(1);
  // Linear candidates with tail stats (QQQ, IWM) + option structures (2).
  expect(bodyRows.length).toBe(4);
  const qqqRow = bodyRows[0];
  expect(within(qqqRow).getByText("QQQ")).toBeInTheDocument();
  // Without: -2.10%; with: -0.80%. The with-hedge number is a book quantity -> amber.
  expect(within(qqqRow).getByTestId("tail-without-cell").textContent).toContain("-2.10%");
  const withCell = within(qqqRow).getByTestId("tail-with-cell");
  expect(withCell.textContent).toContain("-0.80%");
  expect(withCell.className).toMatch(/text-you/);
  // Horizon label: worst-decile benchmark days, daily means (appears in the
  // panel chrome AND the methodology footnote - both are fine).
  expect(screen.getAllByText(/worst-decile/i).length).toBeGreaterThan(0);
});

// --- book_ref pre-load (wave-3B spine): a ?book_ref= URL param pre-loads
// the pinned snapshot into the builder and the next run submits by ref. ---

test("book_ref URL param pre-loads the pinned book and runs by ref", async () => {
  window.history.replaceState(null, "", "/?book_ref=snap-abc123");
  server.use(
    http.get("/api/book/snap-abc123", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/hedge", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; book?: unknown };
      expect(body.book_ref).toBe("snap-abc123");
      expect(body.book).toBeUndefined();
      return HttpResponse.json(HEDGE_RESPONSE);
    })
  );
  renderHedge();
  // Rows are populated from the pinned snapshot without any click.
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  expect(screen.getByDisplayValue("SPY")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("candidates-table");
});

test("symbol input uppercases and add/remove row controls manage the book", async () => {
  renderHedge();
  const [firstSymbol] = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(firstSymbol, { target: { value: "qqq" } });
  expect((firstSymbol as HTMLInputElement).value).toBe("QQQ");

  fireEvent.click(screen.getByRole("button", { name: /add row/i }));
  expect(screen.getAllByLabelText(/symbol/i).length).toBe(2);

  const removeButtons = screen.getAllByRole("button", { name: /remove row/i });
  fireEvent.click(removeButtons[removeButtons.length - 1]);
  expect(screen.getAllByLabelText(/symbol/i).length).toBe(1);
});

test("422 detail surfaces instead of crashing", async () => {
  server.use(
    http.post("/api/hedge", () =>
      HttpResponse.json({ detail: "unknown symbols: ['NOPE']" }, { status: 422 })
    )
  );
  renderHedge();
  const [symbolInput] = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInput, { target: { value: "nope" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  expect(await screen.findByText(/unknown symbols/i)).toBeInTheDocument();
});

// --- book_ref (wave-3 Task A1's book-flow spine): "Load current book" ---
// Generous findBy timeout below: resolving GET /api/book/current through
// MSW's fetch interceptor can take longer than testing-library's 1000ms
// default in this environment, unlike a same-tick state update.
const BOOK_FETCH_TIMEOUT = { timeout: 5000 };

const CURRENT_BOOK = {
  snapshot_id: "snap-abc123",
  valuation_ts: "2026-07-24T00:00:00Z",
  base_currency: "USD",
  positions: [{ symbol: "SPY", qty: 25, con_id: 1, sec_type: "STK", multiplier: 1 }],
};

test("load current book populates rows and runs by book_ref", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/hedge", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; book?: unknown };
      expect(body.book_ref).toBe("snap-abc123");
      expect(body.book).toBeUndefined();
      return HttpResponse.json(HEDGE_RESPONSE);
    })
  );
  renderHedge();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  // qty "25" is unambiguous proof the row came from the fetched book, not
  // the page's default blank row.
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  expect(screen.getByDisplayValue("SPY")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("candidates-table");
});

test("editing a row after loading the current book reverts to inline positions", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/hedge", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; book?: { symbol: string; qty: number }[] };
      expect(body.book_ref).toBeUndefined();
      expect(body.book).toEqual([{ symbol: "QQQ", qty: 25 }]);
      return HttpResponse.json(HEDGE_RESPONSE);
    })
  );
  renderHedge();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  fireEvent.change(screen.getByDisplayValue("SPY"), { target: { value: "qqq" } });

  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("candidates-table");
});
