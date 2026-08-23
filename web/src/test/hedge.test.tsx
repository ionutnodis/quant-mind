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

// The Run button fires POST /api/leverage alongside /api/hedge, so a default
// leverage handler is registered on the server (persists across resetHandlers).
const LEVERAGE_RESPONSE = {
  symbols: ["SPY"],
  n_obs: 250,
  max_drawdown: 0.18,
  drawdown_budget: 0.25,
  leverage_headroom: 1.39,
  diversification_ratio: 1.32,
  book_value: 10000.0,
  gross: 10000.0,
  note: "leverage headroom is assumption-bound scenario leverage — not a safe-leverage guarantee.",
  as_of: "2026-07-24T00:00:00Z",
};

const server = setupServer(http.post("/api/leverage", () => HttpResponse.json(LEVERAGE_RESPONSE)));
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

  // Corr stability is labeled a diagnostic, never presented as the rank;
  // there is no cointegration column (removed, pre-wave-3 consolidation).
  expect(screen.getByText(/diagnostic/i)).toBeInTheDocument();
  expect(screen.queryByText(/coint/i)).not.toBeInTheDocument();
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
// Generous findBy timeout below: resolving POST /api/book/pin through
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
    http.post("/api/book/pin", async ({ request }) => {
      expect(await request.json()).toEqual({});
      return HttpResponse.json(CURRENT_BOOK);
    }),
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
  await screen.findByRole("table");
});

test("editing a row after loading the current book reverts to inline positions", async () => {
  server.use(
    http.post("/api/book/pin", async ({ request }) => {
      expect(await request.json()).toEqual({});
      return HttpResponse.json(CURRENT_BOOK);
    }),
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
  await screen.findByRole("table");
});

test("Run also surfaces the resilience lens (drawdown / leverage headroom / diversification)", async () => {
  server.use(http.post("/api/hedge", () => HttpResponse.json(HEDGE_RESPONSE)));
  renderHedge();

  const symbolInputs = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInputs[0], { target: { value: "spy" } });
  const qtyInputs = screen.getAllByLabelText(/qty/i);
  fireEvent.change(qtyInputs[0], { target: { value: "10" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

  expect(await screen.findByText(/^Resilience$/)).toBeInTheDocument();
  expect(screen.getByText(/18\.00%/)).toBeInTheDocument(); // max drawdown
  expect(screen.getByText(/1\.39×/)).toBeInTheDocument(); // leverage headroom
  expect(screen.getByText(/1\.32/)).toBeInTheDocument(); // diversification ratio
  expect(screen.getByText(/assumption-bound scenario leverage/i)).toBeInTheDocument();
});

test("a failed resilience/leverage request surfaces an error instead of silently vanishing", async () => {
  server.use(
    http.post("/api/hedge", () => HttpResponse.json(HEDGE_RESPONSE)),
    http.post("/api/leverage", () =>
      HttpResponse.json({ detail: "portfolio has zero gross market value" }, { status: 422 })
    )
  );
  renderHedge();
  const symbolInputs = screen.getAllByLabelText(/symbol/i);
  fireEvent.change(symbolInputs[0], { target: { value: "spy" } });
  const qtyInputs = screen.getAllByLabelText(/qty/i);
  fireEvent.change(qtyInputs[0], { target: { value: "10" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

  // candidates still render (hedge ok), but the resilience failure is visible
  expect(await screen.findByRole("table")).toBeInTheDocument();
  expect(await screen.findByText(/Resilience:.*zero gross/i)).toBeInTheDocument();
  expect(screen.queryByText(/^Resilience$/)).not.toBeInTheDocument(); // panel absent
});
