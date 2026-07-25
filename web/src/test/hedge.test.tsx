/**
 * Hedge Lab page tests (Task 3, wave-2 plan): "decisions, not analytics" —
 * objective + book builder -> Run -> ranked candidates table. Protection is
 * the ranking key (amber, per the wave-2 Global Constraints addendum:
 * hypothetical/hedge results ARE the user's book for color purposes);
 * cointegration is a labeled diagnostic column only, never the rank.
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
afterEach(() => server.resetHandlers());
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
  n_candidates_evaluated: 3,
  as_of: "2026-07-24T00:00:00Z",
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
      residual_beta: 0.05,
      corr_stability: 0.04,
      coint_pvalue: 0.031,
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
      residual_beta: 0.1,
      corr_stability: 0.09,
      coint_pvalue: 0.4,
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
      residual_beta: null,
      corr_stability: 0.5,
      coint_pvalue: null,
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

  const table = await screen.findByRole("table");
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

  // Cointegration is labeled a diagnostic, never presented as the rank.
  expect(screen.getByText(/diagnostic/i)).toBeInTheDocument();
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
