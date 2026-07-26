/**
 * Macro page component tests: yields + spread render from the mocked API,
 * a missing block (net liquidity) shows an honest empty state rather than
 * crashing, and sector rows render in the order the API returns them
 * (backend sorts by ret_1d desc) with TradingView links.
 */
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { Macro } from "../pages/Macro";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
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
  as_of: "2026-07-24T00:00:00Z",
  missing: [],
};

test("renders yields, 2s10s spread with sign, and the 10Y series chart", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  expect(await screen.findByText("4.50%")).toBeInTheDocument(); // US 10Y
  expect(screen.getByText("3.80%")).toBeInTheDocument(); // US 2Y
  expect(screen.getByText("5.20%")).toBeInTheDocument(); // US 3M
  expect(screen.getByText("+0.70%")).toBeInTheDocument(); // 2s10s spread, signed
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
  expect(screen.getByText("4.50%")).toBeInTheDocument();
});

test("sector rows render in API order with TradingView links", async () => {
  server.use(http.get("/api/macro", () => HttpResponse.json(FULL)));
  renderMacro();
  await screen.findByText("4.50%");

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
        net_liquidity: null,
        sectors: [],
        factors: [],
        as_of: null,
        missing: ["US10Y", "US2Y", "US3M", "NET_LIQUIDITY", "XLK"],
      })
    )
  );
  renderMacro();
  expect(await screen.findByText(/no yields cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no net liquidity cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no sector data cached yet/i)).toBeInTheDocument();
  expect(screen.getByText(/no factor data cached yet/i)).toBeInTheDocument();
});
