/**
 * Macro page component tests: yields + spread render from the mocked API,
 * a missing block (net liquidity) shows an honest empty state rather than
 * crashing, and sector rows render in the order the API returns them
 * (backend sorts by ret_1d desc) with TradingView links.
 *
 * Wave-3B "Macro book-aware": the amber (text-you) sensitivity column
 * renders ONLY book-sensitivity dollar figures (with CIs) and only when a
 * book_ref is pinned in the URL; without one every sensitivity slot shows
 * the honest "pin a book to see sensitivities" empty state. Curve snapshots
 * (today vs 21d/63d ago, 2s10s highlighted) and VIX-tercile regime rotation
 * render from their blocks and degrade to nothing when null.
 */
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { Macro } from "../pages/Macro";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.history.replaceState(null, "", "/"); // clear any ?book_ref
});
afterAll(() => server.close());

// Plotly needs real canvas/WebGL; stub the chart in jsdom (Risk/Today pattern).
vi.mock("../components/SeriesChart", () => ({
  SeriesChart: () => <div data-testid="series-chart" />,
}));

function renderMacro() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Macro />
    </QueryClientProvider>
  );
}

const SERIES = [
  { date: "2026-07-23T00:00:00Z", value: 0.044 },
  { date: "2026-07-24T00:00:00Z", value: 0.045 },
];

const FULL = {
  yields: {
    us10y: 0.045,
    us2y: 0.038,
    us3m: 0.052,
    spread_2s10s: 0.007,
    series: { us10y: SERIES, us2y: SERIES, us3m: SERIES },
  },
  curve: {
    tenors: [
      { tenor: "US3M", years: 0.25, today: 0.052, m1: 0.05, m3: 0.05 },
      { tenor: "US2Y", years: 2, today: 0.038, m1: 0.03, m3: 0.03 },
      { tenor: "US10Y", years: 10, today: 0.045, m1: 0.04, m3: 0.04 },
    ],
    spread_2s10s_today: 0.007,
    spread_2s10s_m1: 0.01,
    spread_2s10s_m3: 0.01,
    note: "snapshots: today vs 21 and 63 trading days ago",
  },
  net_liquidity: {
    latest_bn: 6100.0,
    series: SERIES,
    cadence_note: "weekly",
  },
  sectors: [
    { symbol: "XLK", ret_1d: 0.01, ret_1m: 0.03, ret_3m: 0.08 },
    { symbol: "XLE", ret_1d: -0.01, ret_1m: -0.02, ret_3m: -0.05 },
  ],
  factors: [
    { symbol: "MTUM", ret_1d: 0.006, ret_1m: 0.02, ret_3m: 0.05 },
    { symbol: "VLUE", ret_1d: -0.004, ret_1m: -0.01, ret_3m: -0.02 },
  ],
  regime_rotation: {
    regime_note: "VIX close terciles over the shared daily sample",
    buckets: [
      {
        bucket: "low",
        lo: 12.1,
        hi: 14.9,
        n_days: 23,
        rows: [
          { symbol: "XLK", mean_daily: 0.012, se_daily: 0.003 },
          { symbol: "XLE", mean_daily: -0.008, se_daily: 0.004 },
        ],
      },
      {
        bucket: "mid",
        lo: 15.0,
        hi: 19.1,
        n_days: 23,
        rows: [
          { symbol: "XLK", mean_daily: 0.002, se_daily: 0.002 },
          { symbol: "XLE", mean_daily: 0.001, se_daily: 0.003 },
        ],
      },
      {
        bucket: "high",
        lo: 19.2,
        hi: 31.0,
        n_days: 23,
        rows: [
          { symbol: "XLE", mean_daily: 0.009, se_daily: 0.002 },
          { symbol: "XLK", mean_daily: -0.011, se_daily: 0.005 },
        ],
      },
    ],
    as_of: "2026-07-24T00:00:00Z",
    note: null,
  },
  sensitivity: null,
  as_of: "2026-07-24T00:00:00Z",
  missing: [],
};

const BOOK_REF = "abcdefabcdef";

const SENSITIVITY = {
  book_ref: BOOK_REF,
  book_gross: 125000,
  excluded: [],
  rows: [
    {
      driver: "US10Y", group: "rates", shock_label: "+10bp",
      dollar_response: -1234.5, se: 300, ci_low: -1834.2, ci_high: -634.8,
      beta: -0.000988, n_obs: 252, note: null,
    },
    {
      driver: "US2Y", group: "rates", shock_label: "+10bp",
      dollar_response: -400, se: 150, ci_low: -700, ci_high: -100,
      beta: -0.0003, n_obs: 252, note: null,
    },
    {
      driver: "XLK", group: "sectors", shock_label: "+1%",
      dollar_response: 310.2, se: 40, ci_low: 230.9, ci_high: 389.5,
      beta: 0.25, n_obs: 252, note: null,
    },
    {
      driver: "VIX", group: "vol", shock_label: "+5 vol pts",
      dollar_response: -2100, se: 500, ci_low: -3100, ci_high: -1100,
      beta: -0.0034, n_obs: 252, note: null,
    },
    {
      driver: "MTUM", group: "factors", shock_label: "+1%",
      dollar_response: null, se: null, ci_low: null, ci_high: null,
      beta: null, n_obs: null, note: "insufficient overlapping observations",
    },
  ],
  window_note:
    "last 252 aligned daily returns (or fewer); Newey-West HAC SEs, 95% CI; linear (delta) approximation",
  as_of: "2026-07-24T00:00:00Z",
  note: null,
};

test("renders yields, 2s10s spread with sign, and the 10Y series chart", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  expect((await screen.findAllByText("4.50%")).length).toBeGreaterThan(0); // US 10Y
  expect(screen.getAllByText("3.80%").length).toBeGreaterThan(0); // US 2Y
  expect(screen.getAllByText("5.20%").length).toBeGreaterThan(0); // US 3M
  expect(screen.getAllByText("+0.70%").length).toBeGreaterThan(0); // 2s10s spread, signed
  expect(screen.getAllByTestId("series-chart").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/as of 2026-07-24/).length).toBeGreaterThan(0);
});

test("missing net-liquidity block shows an honest empty state, not a crash", async () => {
  server.use(
    http.get("/api/macro", () =>
      HttpResponse.json({ ...FULL, net_liquidity: null, missing: ["NET_LIQUIDITY"] })
    )
  );
  renderMacro();
  expect(await screen.findByText(/no net liquidity cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/missing: NET_LIQUIDITY/)).toBeInTheDocument();
  // yields block still renders fine alongside the missing one
  expect(screen.getAllByText("4.50%").length).toBeGreaterThan(0);
});

test("sector rows render in API order with TradingView links", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");

  const symbolCells = screen.getAllByRole("cell", { name: /^XL/ });
  expect(symbolCells.map((c) => c.textContent)).toEqual(["XLK", "XLE"]);

  const links = screen.getAllByRole("link", { name: /chart/i });
  const tvLink = links.find((a) => a.getAttribute("href")?.includes("XLK"));
  expect(tvLink).toBeDefined();
  expect(tvLink).toHaveAttribute("href", "https://www.tradingview.com/chart/?symbol=XLK");
  expect(tvLink).toHaveAttribute("target", "_blank");
});

test("empty-store response (all blocks missing) renders structured empties, not a crash", async () => {
  server.use(
    http.get("/api/macro", () =>
      HttpResponse.json({
        yields: null,
        curve: null,
        net_liquidity: null,
        sectors: [],
        factors: [],
        regime_rotation: null,
        sensitivity: null,
        as_of: null,
        missing: ["US10Y", "US2Y", "US3M", "NET_LIQUIDITY", "XLK", "VIX"],
      })
    )
  );
  renderMacro();
  expect(await screen.findByText(/no yields cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no net liquidity cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no sector data cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no factor data cached yet/i)).toBeInTheDocument();
});

// --- wave-3B: curve snapshots ----------------------------------------------

test("curve panel renders today vs 21d/63d snapshots with 2s10s highlighted", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");

  // snapshot column headers carry the horizon labels
  expect(screen.getByText(/21d ago/i)).toBeInTheDocument();
  expect(screen.getByText(/63d ago/i)).toBeInTheDocument();
  // 21d-ago 10Y value (0.040) renders; today's values collide with the
  // yields block by design (same numbers), hence getAllByText above.
  expect(screen.getAllByText("4.00%").length).toBeGreaterThan(0);
  // 2s10s spread row: today +0.70% (also in yields) and both lagged +1.00%
  expect(screen.getAllByText("+1.00%").length).toBe(2);
  expect(screen.getAllByText(/2s10s/i).length).toBeGreaterThan(0);
});

// --- wave-3B: regime-conditional rotation ----------------------------------

test("regime rotation renders VIX-tercile buckets with mean ± SE per symbol", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");

  expect(screen.getByText(/VIX close terciles/i)).toBeInTheDocument();
  // every estimate carries its SE: mean daily ± SE rendered together
  expect(screen.getByText("+1.20% ±0.30%")).toBeInTheDocument(); // XLK, low bucket
  expect(screen.getByText("-1.10% ±0.50%")).toBeInTheDocument(); // XLK, high bucket
  // bucket sizes (horizon labeling) are visible
  expect(screen.getAllByText(/23d/).length).toBe(3);
});

// --- wave-3B: the amber book-sensitivity column ----------------------------

test("without a pinned book every sensitivity slot shows the honest empty state", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");
  expect(screen.getAllByText(/pin a book to see sensitivities/i).length).toBeGreaterThan(0);
});

test("with ?book_ref pinned, amber sensitivity figures render with CIs", async () => {
  window.history.replaceState(null, "", `/?book_ref=${BOOK_REF}`);
  server.use(
    http.get("/api/macro", ({ request }) => {
      const ref = new URL(request.url).searchParams.get("book_ref");
      // the page must forward the pinned ref from the URL to the API
      if (ref !== BOOK_REF) return HttpResponse.json(FULL);
      return HttpResponse.json({ ...FULL, sensitivity: SENSITIVITY });
    })
  );
  renderMacro();
  await screen.findAllByText("4.50%");

  // rates row: "+10bp → -$1,235 [-$1,834, -$635]" — value + CI, amber only
  const us10y = screen.getByText("-$1,235");
  expect(us10y.className).toContain("text-you");
  expect(screen.getByText(/\[-\$1,834, -\$635\]/)).toBeInTheDocument();

  // sector table's amber column: XLK +1% shock response
  const xlk = screen.getByText("+$310");
  expect(xlk.className).toContain("text-you");

  // vol driver row renders with its labeled shock
  expect(screen.getAllByText(/\+5 vol pts/).length).toBeGreaterThan(0);
  expect(screen.getByText("-$2,100")).toBeInTheDocument();

  // regression window + estimator are labeled (horizon law)
  expect(screen.getAllByText(/last 252 aligned daily returns/i).length).toBeGreaterThan(0);

  // no pin-a-book empty state when the book is pinned
  expect(screen.queryByText(/pin a book to see sensitivities/i)).not.toBeInTheDocument();
});

test("unknown book_ref degrade note renders where the strip would be", async () => {
  // Batch-2 final review item 4: the backend now returns 200 with a
  // top-level note for a stale/unknown ref — the page renders it instead of
  // the generic pin-a-book empty state.
  window.history.replaceState(null, "", `/?book_ref=${BOOK_REF}`);
  server.use(
    http.get("/api/macro", () =>
      HttpResponse.json({
        ...FULL,
        sensitivity: null,
        note: `unknown book_ref '${BOOK_REF}' — re-pin from What-If or Portfolio`,
      })
    )
  );
  renderMacro();
  await screen.findAllByText("4.50%");
  expect(screen.getByText(/re-pin from What-If or Portfolio/i)).toBeInTheDocument();
});

test("sensitivity strip renders even when the yields block is missing", async () => {
  // Batch-2 final review item 7a: the strip/disclosure block must not be
  // gated on yields availability.
  window.history.replaceState(null, "", `/?book_ref=${BOOK_REF}`);
  server.use(
    http.get("/api/macro", () =>
      HttpResponse.json({
        ...FULL,
        yields: null,
        missing: ["US10Y", "US2Y", "US3M"],
        sensitivity: { ...SENSITIVITY, excluded: ["SPY (option leg)"] },
      })
    )
  );
  renderMacro();
  expect(await screen.findByText(/no yields cached yet/i)).toBeInTheDocument();
  // rates/vol strip rows + the excluded-legs disclosure still render
  expect(screen.getByText("-$1,235")).toBeInTheDocument();
  expect(screen.getByText(/excluded: SPY \(option leg\)/i)).toBeInTheDocument();
});

test("Book column header is hidden when no sensitivity block is present", async () => {
  // Batch-2 final review item 7e: a header over an all-dash column implied
  // a broken feature rather than an absent book.
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");
  expect(screen.queryByText(/Book \//i)).not.toBeInTheDocument();
});

test("amber never leaks onto market data: return cells are up/down, not text-you", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findAllByText("4.50%");
  // sector 1D return cell (market data) must use the up/down semantic colors
  const cell = screen.getAllByText(/▲ 1\.00%/)[0];
  expect(cell.className).not.toContain("text-you");
  expect(cell.className).toContain("text-up");
});
