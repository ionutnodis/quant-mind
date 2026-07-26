/**
 * GlanceCharts tests (wave-3B Today task): mini candle charts for SPX/VIX/
 * USO/GLD from the mocked instrument candles endpoint, the 2s10s spread
 * built from /api/macro's yields.series, and honest empty cells when a
 * symbol has no cached candles / macro has no yields.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { GlanceCharts } from "../components/GlanceCharts";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

vi.mock("../components/InstrumentHover", () => ({
  InstrumentHover: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));
vi.mock("../components/CandleChart", () => ({
  CandleChart: () => <div data-testid="candle-chart-stub" />,
}));
vi.mock("../components/SeriesChart", () => ({
  SeriesChart: () => <div data-testid="series-chart-stub" />,
}));

function renderGlance() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <GlanceCharts />
    </QueryClientProvider>
  );
}

const CANDLES = {
  symbol: "SPX",
  days: 90,
  candles: [
    { date: "2026-07-24T00:00:00Z", open: 5000, high: 5010, low: 4990, close: 5005, volume: 1000 },
  ],
};

const MACRO_WITH_YIELDS = {
  yields: {
    spread_2s10s: 0.007,
    series: {
      us10y: [
        { date: "2026-07-23T00:00:00Z", value: 0.045 },
        { date: "2026-07-24T00:00:00Z", value: 0.046 },
      ],
      us2y: [
        { date: "2026-07-23T00:00:00Z", value: 0.038 },
        { date: "2026-07-24T00:00:00Z", value: 0.039 },
      ],
    },
  },
};

test("renders a mini chart cell for each glance instrument with cached candles", async () => {
  server.use(
    http.get("/api/instruments/:symbol/candles", () => HttpResponse.json(CANDLES)),
    http.get("/api/macro", () => HttpResponse.json(MACRO_WITH_YIELDS))
  );
  renderGlance();

  for (const symbol of ["SPX", "VIX", "USO", "GLD"]) {
    expect(await screen.findByTestId(`glance-${symbol}`)).toBeInTheDocument();
  }
  expect((await screen.findAllByTestId("candle-chart-stub")).length).toBe(4);
});

test("renders the 2s10s spread from macro's yields.series", async () => {
  server.use(
    http.get("/api/instruments/:symbol/candles", () => HttpResponse.json({ ...CANDLES, candles: [] })),
    http.get("/api/macro", () => HttpResponse.json(MACRO_WITH_YIELDS))
  );
  renderGlance();
  expect(await screen.findByTestId("series-chart-stub")).toBeInTheDocument();
});

test("honest empty cell when a symbol has no cached candles", async () => {
  server.use(
    http.get("/api/instruments/:symbol/candles", () => HttpResponse.json({ ...CANDLES, candles: [] })),
    http.get("/api/macro", () => HttpResponse.json({ yields: null }))
  );
  renderGlance();
  await waitFor(() => expect(screen.getAllByText(/no cached candles/i).length).toBe(4));
});

test("honest empty spread cell when macro has no yields block", async () => {
  server.use(
    http.get("/api/instruments/:symbol/candles", () => HttpResponse.json({ ...CANDLES, candles: [] })),
    http.get("/api/macro", () => HttpResponse.json({ yields: null }))
  );
  renderGlance();
  expect(await screen.findByText(/no cached yields/i)).toBeInTheDocument();
});
