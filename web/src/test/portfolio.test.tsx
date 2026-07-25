/**
 * Portfolio page component tests: dense positions table renders from the
 * mocked API, totals row, snapshot/valuation-ts note, honest empty state
 * when the paper book has no positions.
 */
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { Portfolio } from "../pages/Portfolio";

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
    },
  ],
  totals: { market_value: 3500.0, n_positions: 2 },
};

const EMPTY = {
  snapshot_id: "empty000000",
  valuation_ts: "2026-07-25T00:00:00Z",
  base_currency: "USD",
  positions: [],
  totals: { market_value: null, n_positions: 0 },
};

test("renders positions table with totals from the API", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(TWO_POSITIONS)));
  renderPortfolio();
  expect(await screen.findByText("SPY")).toBeInTheDocument();
  expect(screen.getByText("OPT_XYZ")).toBeInTheDocument();
  expect(screen.getByText("100.00")).toBeInTheDocument();
  expect(screen.getByText("1000.00")).toBeInTheDocument();
  expect(screen.getByText("2500.00")).toBeInTheDocument();
  // weight formatting
  expect(screen.getByText("28.6%")).toBeInTheDocument();
  expect(screen.getByText("71.4%")).toBeInTheDocument();
  // totals row
  expect(screen.getByText(/Total \(2\)/)).toBeInTheDocument();
  expect(screen.getByText("3500.00")).toBeInTheDocument();
  // snapshot/valuation-ts note
  expect(screen.getByText(/abc123def456/)).toBeInTheDocument();
});

test("null price fields render as em-dash placeholders, not crashes", async () => {
  const withMissingPrice = {
    ...TWO_POSITIONS,
    positions: [
      { con_id: 3, symbol: "UNKNOWN", qty: 3, sec_type: "STK", multiplier: 1, last_close: null, market_value: null, weight: null },
    ],
    totals: { market_value: null, n_positions: 1 },
  };
  server.use(http.get("/api/portfolio", () => HttpResponse.json(withMissingPrice)));
  renderPortfolio();
  expect(await screen.findByText("UNKNOWN")).toBeInTheDocument();
  const dashes = screen.getAllByText("—");
  expect(dashes.length).toBeGreaterThanOrEqual(3); // last, mkt value, weight
});

test("empty paper book shows honest empty state, not a crash", async () => {
  server.use(http.get("/api/portfolio", () => HttpResponse.json(EMPTY)));
  renderPortfolio();
  expect(await screen.findByText(/No positions in the paper book yet/)).toBeInTheDocument();
});
