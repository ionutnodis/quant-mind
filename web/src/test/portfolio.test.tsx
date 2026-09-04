/**
 * Portfolio page component tests: ledger essentials (positions + cost basis
 * + account), delta-adjusted exposure, options sleeve (Greeks + stress
 * grid), expiry buckets, and core-vs-overlay P&L attribution — all rendered
 * from the mocked GET /api/portfolio response. Plotly needs real canvas —
 * PortfolioAttributionChart is stubbed, same pattern as risk.test.tsx stubs
 * FanChart/RollingBetaChart.
 */
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { Portfolio } from "../pages/Portfolio";

vi.mock("../components/PortfolioAttributionChart", () => ({
  PortfolioAttributionChart: () => <div data-testid="portfolio-attribution-chart-stub" />,
}));

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderPortfolio() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Portfolio />
    </QueryClientProvider>
  );
}

const BASE_SLEEVE = { available: false, reason: "no option positions", underlyings: [], stress_grid: null };
const BASE_BUCKETS = { le_7d: [], le_30d: [], le_90d: [], later: [] };
const BASE_ATTRIBUTION = {
  available: false,
  reason: "no priced positions with enough overlapping history for a book return series",
  window_days: 90,
  beta: null,
  n_obs: 0,
  total_pnl: null,
  core_pnl: null,
  overlay_pnl: null,
  core_share: null,
  overlay_share: null,
  series: [],
};

const TWO_POSITIONS = {
  snapshot_id: "abc123def456",
  valuation_ts: "2026-07-25T00:00:00Z",
  base_currency: "USD",
  positions: [
    {
      con_id: 1,
      symbol: "SPY",
      qty: 10,
      sec_type: "STK",
      multiplier: 1,
      last_close: 100.0,
      market_value: 1000.0,
      weight: 0.2857142857142857,
      avg_cost: 90.0,
      unrealized_pnl: 100.0,
    },
    {
      con_id: 2,
      symbol: "OPT_XYZ",
      qty: 5,
      sec_type: "OPT",
      multiplier: 100,
      last_close: 5.0,
      market_value: 2500.0,
      weight: 0.7142857142857143,
      avg_cost: 4.0,
      unrealized_pnl: 500.0,
    },
  ],
  totals: {
    market_value: 3500.0,
    priced_market_value: 3500.0,
    n_positions: 2,
    priced_positions: 2,
    valuation_status: "complete",
    unrealized_pnl: 600.0,
    reported_unrealized_pnl: 600.0,
    pnl_status: "complete",
  },
  account: {
    currency: "USD",
    net_liquidation: 125000.5,
    total_cash_value: 20000.0,
    gross_position_value: 105000.5,
    buying_power: 60000.0,
  },
  account_note: null,
  exposure: [
    {
      underlier: "SPY",
      spot: 100.0,
      net_delta: 10.0,
      dollar_delta: 1000.0,
      beta: 1.0,
      spy_equivalent_notional: 1000.0,
      beta_note: null,
    },
  ],
  options_sleeve: BASE_SLEEVE,
  expiry_buckets: BASE_BUCKETS,
  attribution: BASE_ATTRIBUTION,
};

const EMPTY = {
  snapshot_id: "empty000000",
  valuation_ts: "2026-07-25T00:00:00Z",
  base_currency: "USD",
  positions: [],
  totals: {
    market_value: null,
    priced_market_value: null,
    n_positions: 0,
    priced_positions: 0,
    valuation_status: "empty",
    unrealized_pnl: null,
    reported_unrealized_pnl: null,
    pnl_status: "empty",
  },
  account: null,
  account_note: "NO MATERIAL LINK — no broker connected",
  exposure: [],
  options_sleeve: BASE_SLEEVE,
  expiry_buckets: BASE_BUCKETS,
  attribution: BASE_ATTRIBUTION,
};

test("renders positions table with cost basis, unrealized P&L, and totals from the API", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  const table = within(await screen.findByTestId("positions-table"));
  expect(table.getByText("SPY")).toBeInTheDocument();
  expect(table.getByText("OPT_XYZ")).toBeInTheDocument();
  expect(table.getAllByText("100.00").length).toBeGreaterThan(0); // last_close and unrealized_pnl both 100.00
  expect(table.getByText("1000.00")).toBeInTheDocument();
  expect(table.getByText("2500.00")).toBeInTheDocument();
  // cost basis / unrealized P&L — book P&L renders AMBER regardless of sign
  // (DESIGN.md amber law + Lab Apply-to-Book / WhatIf precedent); green/red
  // stays reserved for market up/down data.
  expect(table.getByText("90.00")).toBeInTheDocument();
  const totalsUnrealized = table.getByTestId("totals-unrealized-pnl");
  expect(totalsUnrealized).toHaveClass("text-you");
  expect(totalsUnrealized).not.toHaveClass("text-up");
  expect(totalsUnrealized).not.toHaveClass("text-down");
  // weight formatting
  expect(table.getByText("28.6%")).toBeInTheDocument();
  expect(table.getByText("71.4%")).toBeInTheDocument();
  // totals row
  expect(table.getByText(/Total \(2\)/)).toBeInTheDocument();
  expect(table.getByText("3500.00")).toBeInTheDocument();
  // snapshot/valuation-ts note (Panel-level, outside the table)
  expect(screen.getByText(/abc123def456/)).toBeInTheDocument();
});

test("null price fields render as em-dash placeholders, not crashes", async () => {
  const withMissingPrice = {
    ...EMPTY,
    positions: [
      {
        con_id: 3, symbol: "UNKNOWN", qty: 3, sec_type: "STK", multiplier: 1,
        last_close: null, market_value: null, weight: null, avg_cost: null, unrealized_pnl: null,
      },
    ],
    totals: {
      market_value: null,
      priced_market_value: null,
      n_positions: 1,
      priced_positions: 0,
      valuation_status: "partial",
      unrealized_pnl: null,
      reported_unrealized_pnl: null,
      pnl_status: "partial",
    },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(withMissingPrice)));
  renderPortfolio();
  expect(await screen.findByText("UNKNOWN")).toBeInTheDocument();
  const dashes = screen.getAllByText("—");
  expect(dashes.length).toBeGreaterThanOrEqual(5); // last, avg cost, unrealized, mkt value, weight
});

test("partial valuations expose priced subtotals without presenting complete portfolio totals", async () => {
  const partial = {
    ...TWO_POSITIONS,
    positions: [
      { ...TWO_POSITIONS.positions[0], weight: null },
      {
        ...TWO_POSITIONS.positions[1],
        last_close: null,
        market_value: null,
        weight: null,
        avg_cost: null,
        unrealized_pnl: null,
      },
    ],
    totals: {
      market_value: null,
      priced_market_value: 1000.0,
      n_positions: 2,
      priced_positions: 1,
      valuation_status: "partial",
      unrealized_pnl: null,
      reported_unrealized_pnl: 100.0,
      pnl_status: "partial",
    },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(partial)));
  renderPortfolio();

  const warning = await screen.findByTestId("portfolio-completeness-warning");
  expect(warning).toHaveTextContent("Pricing incomplete — 1 of 2 positions priced");
  expect(warning).toHaveTextContent("Total market value and portfolio weights are unavailable");
  expect(warning).toHaveTextContent("P&L incomplete");

  const table = within(screen.getByTestId("positions-table"));
  expect(table.queryByText("Total (2)")).not.toBeInTheDocument();
  expect(table.getByText("Priced subtotal (1/2)")).toBeInTheDocument();
  expect(table.getByTestId("totals-market-value")).toHaveTextContent("1000.00");
  expect(table.getByTestId("totals-market-value")).toHaveTextContent("priced only");
  expect(table.getByTestId("totals-unrealized-pnl")).toHaveTextContent("100.00");
  expect(table.getByTestId("totals-unrealized-pnl")).toHaveTextContent("reported only");
});

test("empty paper book shows honest empty state, not a crash", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(EMPTY)));
  renderPortfolio();
  expect(await screen.findByText(/No positions in the paper book yet/)).toBeInTheDocument();
  expect(screen.getByText("NO MATERIAL LINK — no broker connected")).toBeInTheDocument();
});

test("account block renders ledger essentials when the broker reports them", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  expect(await screen.findByText("Net liquidation (USD)")).toBeInTheDocument();
  expect(screen.getByText("Total cash (USD)")).toBeInTheDocument();
  expect(screen.getByText("Gross position value (USD)")).toBeInTheDocument();
  expect(screen.getByText("Buying power (USD)")).toBeInTheDocument();
  expect(screen.getByText("125001")).toBeInTheDocument();
});

test("delta-adjusted exposure table renders per-underlier rows", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  expect(await screen.findByText("Delta-Adjusted Exposure")).toBeInTheDocument();
  const rows = screen.getAllByText("SPY");
  expect(rows.length).toBeGreaterThan(0);
});

test("options sleeve shows honest empty reason when unavailable", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  const reason = await screen.findByText("no option positions");
  expect(reason).toHaveClass("text-market");
  expect(reason).not.toHaveClass("text-warning");
});

test("options sleeve renders greeks table and stress grid when available", async () => {
  const withSleeve = {
    ...TWO_POSITIONS,
    options_sleeve: {
      available: true,
      reason: null,
      underlyings: [{ underlier: "SPY", gamma: 0.01, vega: 12.5, theta: -3.2 }],
      stress_grid: {
        vol_shocks: [0.0],
        spot_shocks: [-0.1, 0.0, 0.1],
        pnl: [[-500, 0, 500]],
      },
    },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(withSleeve)));
  renderPortfolio();
  expect(await screen.findByTestId("portfolio-stress-grid")).toBeInTheDocument();
  expect(screen.getByText("12.50")).toBeInTheDocument(); // vega
});

test("expiry buckets render legs grouped by days-to-expiry", async () => {
  const withBuckets = {
    ...TWO_POSITIONS,
    expiry_buckets: {
      le_7d: [{ symbol: "SPY", expiry: "20260801", right: "C", strike: 105, qty: 1, days_to_expiry: 5 }],
      le_30d: [],
      le_90d: [],
      later: [],
    },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(withBuckets)));
  renderPortfolio();
  expect(await screen.findByText(/SPY C105 20260801/)).toBeInTheDocument();
});

test("attribution shows honest reason when unavailable", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  expect(await screen.findByText(BASE_ATTRIBUTION.reason)).toBeInTheDocument();
});

test("attribution renders core/overlay split and chart when available", async () => {
  const withAttribution = {
    ...TWO_POSITIONS,
    attribution: {
      available: true,
      reason: null,
      window_days: 90,
      beta: 1.2,
      n_obs: 2,
      total_pnl: 400.0,
      core_pnl: 150.0,
      overlay_pnl: 250.0,
      core_share: 0.375,
      overlay_share: 0.625,
      series: [
        { date: "2026-07-01T00:00:00Z", total_pnl: 200.0, core_pnl: 150.0, overlay_pnl: 50.0 },
        { date: "2026-07-02T00:00:00Z", total_pnl: 200.0, core_pnl: 0.0, overlay_pnl: 200.0 },
      ],
    },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(withAttribution)));
  renderPortfolio();
  expect(await screen.findByTestId("portfolio-attribution-chart-stub")).toBeInTheDocument();
  expect(screen.getByText("Core (beta x bench)")).toBeInTheDocument();
  expect(screen.getByText("Overlay (residual)")).toBeInTheDocument();
  // total book P&L is AMBER regardless of sign (DESIGN.md amber law:
  // "P&L attribution" is book data; Lab/WhatIf precedent).
  const total = screen.getByTestId("attribution-total-pnl");
  expect(total).toHaveTextContent("400");
  expect(total).toHaveClass("text-you");
  expect(total).not.toHaveClass("text-up");
  expect(total).not.toHaveClass("text-down");
});
