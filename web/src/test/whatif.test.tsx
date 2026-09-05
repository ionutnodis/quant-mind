/**
 * WhatIf page tests (Task 2, wave-2 plan; book builder swap + book_ref,
 * wave-3 Task A1): position builder posts a hypothetical book to
 * POST /api/whatif; results render in amber (Lab's Apply-to-Book precedent
 * — hypothetical books ARE the user's book for color purposes); 422 detail
 * surfaces honestly, never a crash; named scenarios round-trip through
 * localStorage (server persistence deferred — noted in the panel).
 * No @testing-library/user-event dependency in this repo — fireEvent only
 * (pattern: lab.test.tsx).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "vitest";
import { WhatIf } from "../pages/WhatIf";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.history.replaceState(null, "", "/");
});
afterAll(() => server.close());

beforeEach(() => {
  localStorage.clear();
  server.use(
    http.get("/api/brief", () =>
      HttpResponse.json({
        tiles: [
          { symbol: "SPY", last_close: 100, change_1d: 0.01 },
          { symbol: "QQQ", last_close: 200, change_1d: -0.02 },
        ],
        correlation: null,
        benchmark_es: null,
        as_of: null,
      })
    )
  );
});

function renderWhatIf() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <WhatIf />
    </QueryClientProvider>
  );
}

const WHATIF_RESPONSE = {
  weights: [
    { symbol: "SPY", qty: 60, price: 100, market_value: 6000, weight: 0.6 },
    { symbol: "QQQ", qty: 40, price: 100, market_value: 4000, weight: 0.4 },
  ],
  beta: 1.0234,
  es_975: 0.0311,
  ann_vol: 0.182,
  mc: {
    histogram: { bin_edges: [-0.1, 0, 0.1], counts: [3, 2] },
    p5: -0.09,
    p50: 0.01,
    p95: 0.11,
    n_nonfinite: 0,
  },
  benchmark: { symbol: "SPY", es_975: 0.029, ann_vol: 0.171 },
  n_obs: 250,
  as_of: "2026-07-24T00:00:00Z",
  fx: {
    status: "converted",
    base_currency: "EUR",
    source: "ECB",
    as_of: "2026-07-23",
    fetched_at: "2026-07-23T17:00:00Z",
    missing_currencies: [],
    note: "Prices are normalized to EUR with dated ECB evidence.",
  },
};

function fillFirstRow() {
  fireEvent.change(screen.getByLabelText(/symbol row 1/i), { target: { value: "SPY" } });
  fireEvent.change(screen.getByLabelText(/qty row 1/i), { target: { value: "60" } });
}

function addSecondRow() {
  fireEvent.click(screen.getByRole("button", { name: /add row/i }));
  fireEvent.change(screen.getByLabelText(/symbol row 2/i), { target: { value: "QQQ" } });
  fireEvent.change(screen.getByLabelText(/qty row 2/i), { target: { value: "40" } });
}

test("position builder renders with honest awaiting states before compute", async () => {
  renderWhatIf();
  const compute = await screen.findByRole("button", { name: /^compute$/i });
  expect(compute.closest(".authoring-only-block")).not.toBeNull();
  expect(compute).not.toHaveClass("border-you", "bg-you/10", "text-you");
  expect(screen.getByLabelText(/symbol row 1/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/qty row 1/i)).toBeInTheDocument();
  expect(screen.getAllByText(/awaiting compute/i).length).toBeGreaterThan(0);
});

test("add/remove position rows", async () => {
  renderWhatIf();
  fireEvent.click(await screen.findByRole("button", { name: /add row/i }));
  expect(screen.getByLabelText(/symbol row 2/i)).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText(/remove row 2/i));
  expect(screen.queryByLabelText(/symbol row 2/i)).not.toBeInTheDocument();
});

test("build book -> compute -> amber results render", async () => {
  server.use(http.post("/api/whatif", () => HttpResponse.json(WHATIF_RESPONSE)));
  renderWhatIf();

  fillFirstRow();
  addSecondRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));

  const weights = await screen.findByTestId("whatif-weights");
  expect(within(weights).getByText("SPY")).toBeInTheDocument();
  expect(within(weights).getByText("60.00%")).toBeInTheDocument();
  expect(within(weights).getByText("40.00%")).toBeInTheDocument();

  const bookRisk = screen.getByTestId("whatif-book-risk");
  expect(bookRisk).toHaveClass("text-you");
  expect(within(bookRisk).getByText(/1\.0234/)).toBeInTheDocument();
  expect(within(bookRisk).getByText("3.11%")).toBeInTheDocument();
  expect(within(bookRisk).getByText("18.20%")).toBeInTheDocument();

  const benchRisk = screen.getByTestId("whatif-benchmark-risk");
  expect(within(benchRisk).getByText("2.90%")).toBeInTheDocument();
  expect(within(benchRisk).getByText("17.10%")).toBeInTheDocument();

  const mc = screen.getByTestId("whatif-mc-results");
  expect(mc).toHaveClass("text-you");
  expect(within(mc).getByText("1.00%")).toBeInTheDocument();
});

test("surfaces the What-If FX base, source, and dated evidence", async () => {
  server.use(http.post("/api/whatif", () => HttpResponse.json(WHATIF_RESPONSE)));
  renderWhatIf();
  fillFirstRow();

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));

  expect(await screen.findByTestId("whatif-fx-evidence")).toHaveTextContent(
    "FX base EUR · source ECB · as of 2026-07-23",
  );
});

test("n_nonfinite warning renders when the Monte Carlo drops paths", async () => {
  server.use(
    http.post("/api/whatif", () =>
      HttpResponse.json({ ...WHATIF_RESPONSE, mc: { ...WHATIF_RESPONSE.mc, n_nonfinite: 7 } })
    )
  );
  renderWhatIf();
  fillFirstRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  expect(await screen.findByText(/7 paths produced non-finite/i)).toBeInTheDocument();
});

test("null weight fields render as em-dash placeholders, not crashes", async () => {
  // Backend serialization policy: NaN/Inf -> null. The weights panel must
  // degrade to "—" (portfolio.test.tsx precedent), never crash.
  server.use(
    http.post("/api/whatif", () =>
      HttpResponse.json({
        ...WHATIF_RESPONSE,
        weights: [
          { symbol: "SPY", qty: 60, price: null, market_value: null, weight: null },
          ...WHATIF_RESPONSE.weights.slice(1),
        ],
      })
    )
  );
  renderWhatIf();
  fillFirstRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  const weights = await screen.findByTestId("whatif-weights");
  expect(within(weights).getByText("SPY")).toBeInTheDocument();
  expect(within(weights).getByText("—")).toBeInTheDocument();
  expect(within(weights).getByText("40.00%")).toBeInTheDocument();
});

test("422 detail surfaces honestly, not a crash", async () => {
  server.use(
    http.post("/api/whatif", () =>
      HttpResponse.json({ detail: "unknown symbols: ['NOPE']" }, { status: 422 })
    )
  );
  renderWhatIf();
  fillFirstRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  expect(await screen.findByText(/unknown symbols: \['NOPE'\]/)).toBeInTheDocument();
});

test("scenario save/load round-trip through localStorage", async () => {
  renderWhatIf();
  fillFirstRow();
  addSecondRow();

  fireEvent.change(screen.getByLabelText(/scenario name/i), { target: { value: "my-scenario" } });
  fireEvent.click(screen.getByRole("button", { name: /save scenario/i }));

  expect(localStorage.getItem("quantmind.whatif.scenarios")).toContain("my-scenario");

  // mutate the book after saving
  fireEvent.change(screen.getByLabelText(/qty row 1/i), { target: { value: "999" } });
  expect(screen.getByLabelText(/qty row 1/i)).toHaveValue(999);

  // loading the saved scenario restores the original book
  fireEvent.click(screen.getByRole("button", { name: "my-scenario" }));
  expect(screen.getByLabelText(/qty row 1/i)).toHaveValue(60);
  expect(screen.getByLabelText(/symbol row 2/i)).toHaveValue("QQQ");

  fireEvent.click(screen.getByLabelText(/delete scenario my-scenario/i));
  expect(screen.queryByRole("button", { name: "my-scenario" })).not.toBeInTheDocument();
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

test("load current book populates rows and computes by book_ref", async () => {
  server.use(
    http.post("/api/book/pin", async ({ request }) => {
      expect(await request.json()).toEqual({});
      return HttpResponse.json(CURRENT_BOOK);
    }),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; positions?: unknown };
      expect(body.book_ref).toBe("snap-abc123");
      expect(body.positions).toBeUndefined();
      return HttpResponse.json(WHATIF_RESPONSE);
    })
  );
  renderWhatIf();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  // Replaced the default row (qty 100) with the loaded book's own qty (25),
  // proving the row came from the explicit pin command, not the page default.
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("book_ref query loads and submits the pinned book without showing defaults", async () => {
  window.history.replaceState(null, "", "/whatif?book_ref=url-snapshot");
  server.use(
    http.get("/api/book/url-snapshot", async () => {
      await delay(50);
      return HttpResponse.json({ ...CURRENT_BOOK, snapshot_id: "url-snapshot" });
    }),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; positions?: unknown };
      expect(body.book_ref).toBe("url-snapshot");
      expect(body.positions).toBeUndefined();
      return HttpResponse.json(WHATIF_RESPONSE);
    }),
  );

  renderWhatIf();

  expect(screen.queryByDisplayValue("100")).not.toBeInTheDocument();
  expect(await screen.findByText("Loading pinned book url-snapshot…")).toBeInTheDocument();
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  expect(screen.getByDisplayValue("SPY")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("editing a row after loading the current book reverts to inline positions", async () => {
  server.use(
    http.post("/api/book/pin", async ({ request }) => {
      expect(await request.json()).toEqual({});
      return HttpResponse.json(CURRENT_BOOK);
    }),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as {
        book_ref?: string;
        positions?: { symbol: string; qty: number }[];
      };
      expect(body.book_ref).toBeUndefined();
      expect(body.positions).toEqual([{ symbol: "QQQ", qty: 25 }]);
      return HttpResponse.json(WHATIF_RESPONSE);
    })
  );
  renderWhatIf();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  fireEvent.change(screen.getByLabelText(/symbol row 1/i), { target: { value: "qqq" } });

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});
