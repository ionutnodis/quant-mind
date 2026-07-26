/**
 * InstrumentHover + InstrumentSheet tests (Task A2): hover shows the
 * name/type/exchange/1d/vol/beta tooltip from the mocked /api/instruments
 * endpoint, click opens the InstrumentSheet floating window with the candle
 * chart (stubbed — Plotly needs real canvas/WebGL in jsdom, pattern:
 * macro.test.tsx) + stats + TradingView/issuer link-outs, and missing
 * metadata renders an honest fallback rather than crashing.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { InstrumentHover } from "../components/InstrumentHover";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

vi.mock("../components/CandleChart", () => ({
  CandleChart: () => <div data-testid="candle-chart" />,
}));

function renderHover(props: Partial<React.ComponentProps<typeof InstrumentHover>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InstrumentHover symbol="EEM" change1d={0.012} {...props}>
        EEM
      </InstrumentHover>
    </QueryClientProvider>
  );
}

const INSTRUMENT = {
  symbol: "EEM",
  con_id: 2,
  long_name: "iShares MSCI Emerging Markets ETF",
  exchange: "ARCA",
  currency: "USD",
  sec_type: "STK",
  industry: null,
  region: "Emerging Markets",
  provider: "ibkr",
  last_close: 42.5,
  high_52w: 45.0,
  low_52w: 38.0,
  pct_from_52w_high: -0.0556,
  pct_from_52w_low: 0.1184,
  ann_vol: 0.21,
  beta: 0.85,
  beta_window_days: 60,
  beta_benchmark: "SPY",
  as_of: "2026-07-24T00:00:00Z",
};

const CANDLES = {
  symbol: "EEM",
  days: 180,
  candles: [
    { date: "2026-07-23T00:00:00Z", open: 42.0, high: 43.0, low: 41.5, close: 42.5, volume: 1000.0 },
    { date: "2026-07-24T00:00:00Z", open: 42.5, high: 43.5, low: 42.0, close: 43.0, volume: 1100.0 },
  ],
};

test("hovering the trigger shows name/type/exchange/1d/vol/beta tooltip", async () => {
  server.use(http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)));
  renderHover();

  const trigger = screen.getByTestId("instrument-trigger-EEM");
  fireEvent.mouseEnter(trigger.parentElement!);

  expect(await screen.findByText("iShares MSCI Emerging Markets ETF")).toBeInTheDocument();
  expect(screen.getByText(/ARCA/)).toBeInTheDocument();
  expect(screen.getByText("1.20%")).toBeInTheDocument(); // 1D from change1d prop
  expect(screen.getByText("21.00%")).toBeInTheDocument(); // ann vol
  expect(screen.getByText("0.85")).toBeInTheDocument(); // beta
  // beta labeled with the window the estimate ACTUALLY used (F8)
  expect(screen.getByText(/β·SPY \(60d\)/)).toBeInTheDocument();

  fireEvent.mouseLeave(trigger.parentElement!);
  await waitFor(() => expect(screen.queryByTestId("instrument-hover-EEM")).not.toBeInTheDocument());
});

test("tooltip renders honestly when instrument metadata is missing (nulls)", async () => {
  server.use(
    http.get("/api/instruments/EEM", () =>
      HttpResponse.json({
        ...INSTRUMENT,
        long_name: null,
        exchange: null,
        currency: null,
        ann_vol: null,
        beta: null,
        beta_window_days: null,
      })
    )
  );
  renderHover({ change1d: null });
  fireEvent.mouseEnter(screen.getByTestId("instrument-trigger-EEM").parentElement!);

  expect(await screen.findByText("No metadata cached yet")).toBeInTheDocument();
  // Multiple "—" placeholders render for the missing 1d/vol/beta fields — no crash.
  expect(screen.getAllByText("—").length).toBeGreaterThan(0);
});

test("clicking the trigger opens InstrumentSheet with chart, stats, and link-outs", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json(CANDLES))
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  const sheet = await screen.findByTestId("instrument-sheet-EEM");
  expect(sheet).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("candle-chart")).toBeInTheDocument());

  expect(screen.getAllByText(/iShares MSCI Emerging Markets ETF/).length).toBeGreaterThan(0);
  const tvLink = screen.getByRole("link", { name: /TradingView/i });
  expect(tvLink).toHaveAttribute("href", "https://www.tradingview.com/chart/?symbol=EEM");
  expect(tvLink).toHaveAttribute("target", "_blank");
  const issuerLink = screen.getByRole("link", { name: /Issuer/i });
  expect(issuerLink.getAttribute("href")).toContain("google.com/finance/quote/EEM");

  // hovering while the sheet is open must not also show the hover tooltip
  expect(screen.queryByTestId("instrument-hover-EEM")).not.toBeInTheDocument();

  fireEvent.click(screen.getByTestId("instrument-sheet-close"));
  await waitFor(() => expect(screen.queryByTestId("instrument-sheet-EEM")).not.toBeInTheDocument());
});

test("sheet shows an honest empty state when no candles are cached", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json({ ...CANDLES, candles: [] }))
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  expect(await screen.findByText(/no cached candles yet/i)).toBeInTheDocument();
});
