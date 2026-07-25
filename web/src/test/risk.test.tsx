/**
 * Risk page component tests: symbol picker + rolling beta/alpha/ES/vol stat
 * block render from the mocked /api/risk/:symbol endpoint; Monte Carlo panel
 * starts in an honest awaiting state and renders histogram + percentiles
 * after "Run"; bounded controls carry min/max matching backend Field bounds;
 * empty cache renders a structured empty state, never a crash.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { Risk } from "../pages/Risk";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// Plotly needs real canvas/WebGL; stub the charts in jsdom (today.test.tsx pattern).
vi.mock("../components/RollingBetaChart", () => ({
  RollingBetaChart: () => <div data-testid="rolling-beta-chart" />,
}));
vi.mock("../components/FanChart", () => ({
  FanChart: () => <div data-testid="fan-chart" />,
}));

function renderRisk() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Risk />
    </QueryClientProvider>
  );
}

const BRIEF = {
  tiles: [
    { symbol: "SPY", last_close: 736.28, change_1d: 0.001 },
    { symbol: "QQQ", last_close: 682.41, change_1d: -0.0112 },
  ],
  correlation: { symbols: ["QQQ", "SPY"], matrix: [[1, 0.95], [0.95, 1]] },
  benchmark_es: 0.0314,
  as_of: "2026-07-24T00:00:00Z",
};

const RISK_SPY = {
  symbol: "SPY",
  benchmark: "SPY",
  window: 60,
  years: 5,
  n_obs: 250,
  beta_series: [
    { date: "2026-07-20T00:00:00Z", beta: 1.0 },
    { date: "2026-07-21T00:00:00Z", beta: 1.02 },
  ],
  alpha_annualized: 0.012,
  alpha_note: "vs SPY, rf=0 until FRED wiring",
  es_975: 0.0314,
  ann_vol: 0.182,
  as_of: "2026-07-24T00:00:00Z",
};

const MC_SPY = {
  symbol: "SPY",
  horizon: 252,
  n_paths: 10000,
  histogram: { bin_edges: [-0.1, -0.05, 0, 0.05, 0.1], counts: [10, 40, 30, 20] },
  p5: -0.08,
  p50: 0.01,
  p95: 0.12,
  es_975: 0.095,
};

test("renders symbol picker and rolling beta/ES/vol stats from the API", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY))
  );
  renderRisk();
  expect(await screen.findByTestId("rolling-beta-chart")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: /symbol/i })).toBeInTheDocument();
  expect(screen.getByText("SPY", { selector: "option" })).toBeInTheDocument();
  expect(screen.getByText("QQQ", { selector: "option" })).toBeInTheDocument();
  expect(screen.getByText(/3\.14%/)).toBeInTheDocument(); // ES
  expect(screen.getByText(/18\.20%/)).toBeInTheDocument(); // ann vol
  expect(screen.getByText(/vs SPY, rf=0 until FRED wiring/)).toBeInTheDocument();
  expect(screen.getByText(/symbol lens now/i)).toBeInTheDocument();
});

test("empty cache shows structured empty state, not a crash", async () => {
  server.use(
    http.get("/api/brief", () =>
      HttpResponse.json({ tiles: [], correlation: null, benchmark_es: null, as_of: null })
    )
  );
  renderRisk();
  expect(await screen.findByText(/no market data cached/i)).toBeInTheDocument();
});

test("Monte Carlo panel awaits a run, then shows histogram and percentiles", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY)),
    http.post("/api/risk/montecarlo", () => HttpResponse.json(MC_SPY))
  );
  renderRisk();
  await screen.findByTestId("rolling-beta-chart");
  expect(screen.getByText(/run to see the terminal distribution/i)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  expect(await screen.findByTestId("fan-chart")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText(/12\.00%/)).toBeInTheDocument()); // p95
  expect(screen.getByText(/9\.50%/)).toBeInTheDocument(); // ES of simulated terminal returns
});

test("Monte Carlo 422 surfaces the server's detail message near the controls, not a bare status", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY)),
    http.post("/api/risk/montecarlo", () =>
      HttpResponse.json(
        { detail: "simulation produced no finite terminal returns — check cached bars for zero/degenerate prices" },
        { status: 422 }
      )
    )
  );
  renderRisk();
  await screen.findByTestId("rolling-beta-chart");

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  expect(await screen.findByText(/no finite terminal returns/i)).toBeInTheDocument();
  expect(screen.queryByText(/→ 422/)).not.toBeInTheDocument();
});

test("Risk series 422 surfaces the server's detail message near the beta panel", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () =>
      HttpResponse.json({ detail: "symbol 'SPY' has no cached bars" }, { status: 422 })
    )
  );
  renderRisk();
  expect(await screen.findByText(/no cached bars/i)).toBeInTheDocument();
  expect(screen.queryByText(/→ 422/)).not.toBeInTheDocument();
});

test("window/years/horizon/n_paths controls are bounded matching backend limits", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY))
  );
  renderRisk();
  await screen.findByTestId("rolling-beta-chart");

  const windowInput = screen.getByLabelText(/^window/i) as HTMLInputElement;
  expect(windowInput.min).toBe("5");
  expect(windowInput.max).toBe("756");

  const yearsInput = screen.getByLabelText(/^years/i) as HTMLInputElement;
  expect(yearsInput.min).toBe("1");
  expect(yearsInput.max).toBe("25");

  const horizonInput = screen.getByLabelText(/^horizon/i) as HTMLInputElement;
  expect(horizonInput.min).toBe("1");
  expect(horizonInput.max).toBe("2520");

  const pathsInput = screen.getByLabelText(/^paths/i) as HTMLInputElement;
  expect(pathsInput.min).toBe("1");
  expect(pathsInput.max).toBe("200000");
});
