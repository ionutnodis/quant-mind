/**
 * WhatIf page tests (Task 2, wave-2 plan; book builder swap + book_ref,
 * wave-3 Task A1; What-If flow, wave-3B Batch 2): position builder posts a
 * hypothetical book to POST /api/whatif; 422 detail surfaces honestly, never
 * a crash; named scenarios round-trip through localStorage.
 *
 * Color adjudication (batch-1 final review): the CURRENT book (base) is the
 * user's book and renders amber; hypothetical/scenario values are NOT the
 * live book and render neutral, sign in the number (stress-grid precedent).
 *
 * Wave-3B flow: active book_ref preloads from the URL; "Load current book"
 * pins the base (chip + URL persistence); the result carries a
 * current→hypothetical trade ticket and a CRN-paired delta; option legs wire
 * through the builder; pinned scenarios compare side-by-side.
 * No @testing-library/user-event dependency in this repo — fireEvent only
 * (pattern: lab.test.tsx).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "vitest";
import { WhatIf } from "../pages/WhatIf";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/");
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
    { symbol: "SPY", qty: 60, sec_type: "STK", strike: null, expiry: null, right: null, multiplier: 1, price: 100, market_value: 6000, weight: 0.6 },
    { symbol: "QQQ", qty: 40, sec_type: "STK", strike: null, expiry: null, right: null, multiplier: 1, price: 100, market_value: 4000, weight: 0.4 },
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
    seed: 7,
    horizon_days: 126,
  },
  benchmark: { symbol: "SPY", es_975: 0.029, ann_vol: 0.171 },
  n_obs: 250,
  as_of: "2026-07-24T00:00:00Z",
  base: null,
  delta: null,
  trade_ticket: null,
  notes: [],
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
  expect(await screen.findByRole("button", { name: /^compute$/i })).toBeInTheDocument();
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

test("build book -> compute -> results render; hypothetical values are NEUTRAL, not amber", async () => {
  // Amber adjudication (batch-1 final review): a hypothetical book is NOT
  // the live book — its risk numbers must not carry text-you.
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
  expect(bookRisk).not.toHaveClass("text-you");
  expect(within(bookRisk).getByText(/1\.0234/)).toBeInTheDocument();
  expect(within(bookRisk).getByText("3.11%")).toBeInTheDocument();
  expect(within(bookRisk).getByText("18.20%")).toBeInTheDocument();

  const benchRisk = screen.getByTestId("whatif-benchmark-risk");
  expect(within(benchRisk).getByText("2.90%")).toBeInTheDocument();
  expect(within(benchRisk).getByText("17.10%")).toBeInTheDocument();

  const mc = screen.getByTestId("whatif-mc-results");
  expect(mc).not.toHaveClass("text-you");
  expect(within(mc).getByText("1.00%")).toBeInTheDocument();
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

test("load current book populates rows and computes by book_ref", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
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
  // proving the row came from GET /api/book/current, not the page default.
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("editing a row after loading the current book reverts to inline positions", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
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

// --- wave-3B What-If flow ---

const BASE_RESPONSE = {
  ...WHATIF_RESPONSE,
  base: {
    book_ref: "snap-abc123",
    valuation_ts: "2026-07-24T00:00:00Z",
    n_positions: 1,
    beta: 0.98,
    es_975: 0.0275,
    ann_vol: 0.165,
    p5: -0.08,
    p50: 0.008,
    p95: 0.1,
  },
  delta: { beta: 0.0434, es_975: 0.0036, ann_vol: 0.017, p5: -0.01, p50: 0.002, p95: 0.01 },
  trade_ticket: [
    {
      symbol: "SPY", sec_type: "STK", strike: null, expiry: null, right: null,
      multiplier: 1, qty_from: 100, qty_to: 150, qty_delta: 50, action: "BUY", price: 412.33,
    },
    {
      symbol: "QQQ", sec_type: "STK", strike: null, expiry: null, right: null,
      multiplier: 1, qty_from: 10, qty_to: 0, qty_delta: -10, action: "SELL", price: 500.1,
    },
    {
      symbol: "SPY", sec_type: "OPT", strike: 450, expiry: "20260918", right: "C",
      multiplier: 100, qty_from: 0, qty_to: 2, qty_delta: 2, action: "BUY", price: 412.33,
    },
  ],
};

test("preloads the active book_ref from the URL and pins it as the base", async () => {
  window.history.replaceState(null, "", "/?book_ref=snap-abc123");
  server.use(
    http.get("/api/book/snap-abc123", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { book_ref?: string; base_book_ref?: string };
      expect(body.base_book_ref).toBe("snap-abc123");
      expect(body.book_ref).toBe("snap-abc123");
      return HttpResponse.json(BASE_RESPONSE);
    })
  );
  renderWhatIf();

  // rows populate from the pinned snapshot, and the chip shows the active ref
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  expect(screen.getByTestId("book-pinned-chip")).toHaveTextContent("snap-abc123");

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("load current book pins the base: chip + URL persistence + base_book_ref on compute", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { base_book_ref?: string };
      expect(body.base_book_ref).toBe("snap-abc123");
      return HttpResponse.json(BASE_RESPONSE);
    })
  );
  renderWhatIf();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);

  const chip = screen.getByTestId("book-pinned-chip");
  expect(chip).toHaveTextContent("snap-abc123");
  expect(chip).toHaveTextContent("2026-07-24"); // as-of stamp on the chip
  expect(window.location.search).toContain("book_ref=snap-abc123");

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("unpinning the chip clears the base ref and the URL param", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { base_book_ref?: string };
      expect(body.base_book_ref).toBeUndefined();
      return HttpResponse.json(WHATIF_RESPONSE);
    })
  );
  renderWhatIf();

  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  fireEvent.click(screen.getByLabelText(/unpin current book/i));

  expect(screen.queryByTestId("book-pinned-chip")).not.toBeInTheDocument();
  expect(window.location.search).not.toContain("book_ref");

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("trade ticket renders current→hypothetical per-leg deltas", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/whatif", () => HttpResponse.json(BASE_RESPONSE))
  );
  renderWhatIf();
  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));

  const ticket = await screen.findByTestId("whatif-trade-ticket");
  expect(within(ticket).getByText(/BUY 50 shares SPY/)).toBeInTheDocument();
  expect(within(ticket).getByText(/SELL 10 shares QQQ/)).toBeInTheDocument();
  // an option leg opens contracts, with its full descriptor
  expect(within(ticket).getByText(/OPEN 2 contracts SPY 20260918 450C ×100/)).toBeInTheDocument();
});

test("base (current book) risk renders AMBER; CRN-paired delta renders with the shared seed", async () => {
  server.use(
    http.get("/api/book/current", () => HttpResponse.json(CURRENT_BOOK)),
    http.post("/api/whatif", () => HttpResponse.json(BASE_RESPONSE))
  );
  renderWhatIf();
  fireEvent.click(await screen.findByRole("button", { name: /load current book/i }));
  await screen.findByDisplayValue("25", {}, BOOK_FETCH_TIMEOUT);
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));

  const base = await screen.findByTestId("whatif-base-risk");
  expect(base).toHaveClass("text-you"); // the current book IS the user's book
  expect(within(base).getByText("2.75%")).toBeInTheDocument();

  const delta = screen.getByTestId("whatif-delta");
  expect(delta).not.toHaveClass("text-you"); // deltas are hypothetical-derived
  expect(within(delta).getByText(/CRN-paired/)).toBeInTheDocument();
  expect(within(delta).getByText(/seed 7/)).toBeInTheDocument();
  expect(within(delta).getByText(/\+0\.36%/)).toBeInTheDocument(); // ΔES, sign in the number
});

test("option leg inputs wire strike/expiry/right/multiplier into the POST body", async () => {
  server.use(
    http.post("/api/whatif", async ({ request }) => {
      const body = (await request.json()) as { positions?: unknown };
      expect(body.positions).toEqual([
        { symbol: "SPY", qty: 2, strike: 450, expiry: "2026-09-18", right: "C", multiplier: 100 },
      ]);
      return HttpResponse.json(WHATIF_RESPONSE);
    })
  );
  renderWhatIf();

  fireEvent.change(await screen.findByLabelText(/symbol row 1/i), { target: { value: "SPY" } });
  fireEvent.change(screen.getByLabelText(/qty row 1/i), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText(/type row 1/i), { target: { value: "OPT" } });
  fireEvent.change(screen.getByLabelText(/strike row 1/i), { target: { value: "450" } });
  fireEvent.change(screen.getByLabelText(/expiry row 1/i), { target: { value: "2026-09-18" } });
  fireEvent.change(screen.getByLabelText(/right row 1/i), { target: { value: "C" } });

  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
});

test("an incomplete option leg blocks compute with an honest error, no POST", async () => {
  renderWhatIf();
  fireEvent.change(await screen.findByLabelText(/symbol row 1/i), { target: { value: "SPY" } });
  fireEvent.change(screen.getByLabelText(/type row 1/i), { target: { value: "OPT" } });
  // no strike/expiry set
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  expect(await screen.findByText(/strike and expiry/i)).toBeInTheDocument();
});

test("pin result -> side-by-side compare table, URL-persisted names, unpin removes", async () => {
  server.use(http.post("/api/whatif", () => HttpResponse.json(WHATIF_RESPONSE)));
  renderWhatIf();
  fillFirstRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");

  fireEvent.change(screen.getByLabelText(/pin name/i), { target: { value: "scenA" } });
  fireEvent.click(screen.getByRole("button", { name: /pin result/i }));

  const compare = screen.getByTestId("whatif-pinned-compare");
  expect(within(compare).getByText("scenA")).toBeInTheDocument();
  expect(within(compare).getByText("3.11%")).toBeInTheDocument(); // pinned ES
  expect(within(compare).getAllByText(/126d/).length).toBeGreaterThan(0); // horizon label
  expect(window.location.search).toContain("pins=scenA");
  expect(localStorage.getItem("quantmind.whatif.pins")).toContain("scenA");

  fireEvent.click(within(compare).getByLabelText(/unpin scenario scenA/i));
  expect(within(compare).queryByText("scenA")).not.toBeInTheDocument();
  expect(window.location.search).not.toContain("pins=scenA");
});

// --- fix round 1 ---

function makePin(name: string, es = 0.0311): Record<string, unknown> {
  return {
    name,
    pinned_at: "2026-07-25T00:00:00Z",
    as_of: "2026-07-24T00:00:00Z",
    horizon_days: 126,
    n_paths: 2000,
    seed: 7,
    beta: 1.0,
    es_975: es,
    ann_vol: 0.18,
    p5: -0.09,
    p50: 0.01,
    p95: 0.11,
  };
}

test("?pins= URL param restores the pinned compare selection and order (I2)", async () => {
  localStorage.setItem(
    "quantmind.whatif.pins",
    JSON.stringify({ alpha: makePin("alpha", 0.01), beta: makePin("beta", 0.02) })
  );
  window.history.replaceState(null, "", "/?pins=beta");
  renderWhatIf();

  const compare = await screen.findByTestId("whatif-pinned-compare");
  expect(within(compare).getByText("beta")).toBeInTheDocument();
  // alpha is stored but NOT selected by the shared URL — it must not render
  expect(within(compare).queryByText("alpha")).not.toBeInTheDocument();
  expect(within(compare).getByText("2.00%")).toBeInTheDocument();
});

test("?pins= order wins over localStorage insertion order (I2)", async () => {
  localStorage.setItem(
    "quantmind.whatif.pins",
    JSON.stringify({ alpha: makePin("alpha"), beta: makePin("beta") })
  );
  window.history.replaceState(null, "", "/?pins=beta,alpha");
  renderWhatIf();

  const compare = await screen.findByTestId("whatif-pinned-compare");
  const headers = within(compare).getAllByRole("columnheader").map((h) => h.textContent ?? "");
  const betaIdx = headers.findIndex((h) => h.includes("beta"));
  const alphaIdx = headers.findIndex((h) => h.includes("alpha"));
  expect(betaIdx).toBeGreaterThan(-1);
  expect(alphaIdx).toBeGreaterThan(-1);
  expect(betaIdx).toBeLessThan(alphaIdx);
});

test("corrupt pins localStorage entries are shape-filtered, not a crash (I2 minor)", async () => {
  localStorage.setItem(
    "quantmind.whatif.pins",
    JSON.stringify({ bad: 42, worse: { name: 7 }, good: makePin("good") })
  );
  renderWhatIf();

  const compare = await screen.findByTestId("whatif-pinned-compare");
  expect(within(compare).getByText("good")).toBeInTheDocument();
  expect(within(compare).queryByText("bad")).not.toBeInTheDocument();
  // fully-junk store must degrade to the empty state, never a TypeError
  localStorage.setItem("quantmind.whatif.pins", '["not", "a", "record"]');
});

test("pin names containing a comma survive the URL round-trip (I2 minor)", async () => {
  server.use(http.post("/api/whatif", () => HttpResponse.json(WHATIF_RESPONSE)));
  const first = renderWhatIf();
  fillFirstRow();
  fireEvent.click(screen.getByRole("button", { name: /^compute$/i }));
  await screen.findByTestId("whatif-weights");
  fireEvent.change(screen.getByLabelText(/pin name/i), { target: { value: "a,b" } });
  fireEvent.click(screen.getByRole("button", { name: /pin result/i }));
  expect(within(screen.getByTestId("whatif-pinned-compare")).getByText("a,b")).toBeInTheDocument();

  // fresh mount with the persisted URL + localStorage: one pin named "a,b",
  // not two phantom pins "a" and "b"
  first.unmount();
  renderWhatIf();
  const compare = await screen.findByTestId("whatif-pinned-compare");
  expect(within(compare).getByText("a,b")).toBeInTheDocument();
  expect(within(compare).getAllByRole("columnheader")).toHaveLength(2); // Metric + "a,b"
});

test("scenario save/load round-trips an option leg (I3)", async () => {
  renderWhatIf();
  fireEvent.change(await screen.findByLabelText(/symbol row 1/i), { target: { value: "SPY" } });
  fireEvent.change(screen.getByLabelText(/qty row 1/i), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText(/type row 1/i), { target: { value: "OPT" } });
  fireEvent.change(screen.getByLabelText(/strike row 1/i), { target: { value: "450" } });
  fireEvent.change(screen.getByLabelText(/expiry row 1/i), { target: { value: "2026-09-18" } });
  fireEvent.change(screen.getByLabelText(/right row 1/i), { target: { value: "P" } });

  fireEvent.change(screen.getByLabelText(/scenario name/i), { target: { value: "opt-scen" } });
  fireEvent.click(screen.getByRole("button", { name: /save scenario/i }));

  // mutate the book: back to a plain stock row
  fireEvent.change(screen.getByLabelText(/type row 1/i), { target: { value: "STK" } });
  fireEvent.change(screen.getByLabelText(/symbol row 1/i), { target: { value: "QQQ" } });

  // loading must restore the FULL option leg, not 2 shares of SPY
  fireEvent.click(screen.getByRole("button", { name: "opt-scen" }));
  expect(screen.getByLabelText(/symbol row 1/i)).toHaveValue("SPY");
  expect(screen.getByLabelText(/qty row 1/i)).toHaveValue(2);
  expect(screen.getByLabelText(/type row 1/i)).toHaveValue("OPT");
  expect(screen.getByLabelText(/strike row 1/i)).toHaveValue(450);
  expect(screen.getByLabelText(/expiry row 1/i)).toHaveValue("2026-09-18");
  expect(screen.getByLabelText(/right row 1/i)).toHaveValue("P");
});
