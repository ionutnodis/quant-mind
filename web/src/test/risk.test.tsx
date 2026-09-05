/**
 * Risk page component tests: the decomposition workbench. Symbol picker +
 * factor builder (primary factor defaults to the benchmark once known, chips
 * add more factors) drive GET /api/risk/:symbol/regression; fit stats,
 * per-factor betas, variance decomposition, R^2 progression and return
 * attribution render from that response. Rolling-beta context panel reads
 * three GET /api/risk/:symbol windows (20/60/120) plus a full-sample
 * regression call for the long-run reference line. The horizon-risk panel
 * starts in an honest awaiting state, computes the historical sqrt-t ES
 * client-side, and renders the MC-bootstrap histogram + ES after "Run".
 * Bounded controls carry min/max matching backend Field/Query bounds; empty
 * cache and every 422 path render structured states, never a crash.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
vi.mock("../components/RegressionScatter", () => ({
  RegressionScatter: () => <div data-testid="regression-scatter" />,
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
    { symbol: "MTUM", last_close: 210.5, change_1d: 0.004 },
  ],
  correlation: { symbols: ["MTUM", "QQQ", "SPY"], matrix: [[1, 0.9, 0.8], [0.9, 1, 0.95], [0.8, 0.95, 1]] },
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
  alpha_note: "excess-return Jensen alpha vs SPY, rf=US3M/252",
  es_975: 0.0314,
  ann_vol: 0.182,
  mean_arith_annual: 0.20,
  cagr: 0.185,
  drag_exact: 0.015,
  drag_approx: 0.016562,
  drag_note: "equity sleeve, per-symbol; drag = mean - CAGR ~= 1/2 sigma^2",
  as_of: "2026-07-24T00:00:00Z",
  fx: {
    status: "converted",
    base_currency: "USD",
    source: "ECB",
    as_of: "2026-07-22",
    fetched_at: "2026-07-22T17:00:00Z",
    missing_currencies: [],
    note: "Prices are normalized to USD with dated ECB evidence.",
  },
};

const REGRESSION_SINGLE = {
  symbol: "SPY",
  factors: ["SPY"],
  window: null,
  years: 5,
  n_obs: 250,
  hac_lags: 4,
  scatter: [
    { date: "2026-07-20T00:00:00Z", asset: 0.01, factor: 0.01 },
    { date: "2026-07-21T00:00:00Z", asset: -0.005, factor: -0.004 },
  ],
  fit_line: { factor: "SPY", slope: 1.0, slope_se: 0.02, slope_ci: [0.96, 1.04], intercept: 0.0001, r_squared: 0.98 },
  alpha_daily: 0.0001,
  alpha_annualized: 0.0252,
  alpha_se: 0.00005,
  alpha_ci: [0.00002, 0.00018],
  alpha_tstat: 2.0,
  information_ratio: 1.5,
  betas: [{ factor: "SPY", beta: 1.0, se: 0.02, ci_low: 0.96, ci_high: 1.04 }],
  r_squared: 0.98,
  r_squared_progression: [{ factor_added: "SPY", r_squared: 0.98 }],
  variance_decomposition: [
    { name: "SPY", share: 0.98 },
    { name: "idiosyncratic", share: 0.02 },
  ],
  attribution: [
    { name: "alpha", daily: 0.0001, annualized: 0.0252 },
    { name: "SPY", daily: 0.0003, annualized: 0.0756 },
    { name: "idiosyncratic", daily: 0.0, annualized: 0.0 },
  ],
  alpha_note: "excess-return Jensen alpha vs SPY, rf=US3M/252",
  as_of: "2026-07-24T00:00:00Z",
  horizon_note: "daily returns; alpha/attribution figures shown daily and annualized (x252); n_obs is the full 5y cache",
  fx: {
    status: "converted",
    base_currency: "USD",
    source: "ECB",
    as_of: "2026-07-23",
    fetched_at: "2026-07-23T17:00:00Z",
    missing_currencies: [],
    note: "Prices are normalized to USD with dated ECB evidence.",
  },
};

const REGRESSION_MULTI = {
  ...REGRESSION_SINGLE,
  factors: ["SPY", "MTUM"],
  r_squared: 0.985,
  betas: [
    { factor: "SPY", beta: 0.9, se: 0.03, ci_low: 0.84, ci_high: 0.96 },
    { factor: "MTUM", beta: 0.15, se: 0.02, ci_low: 0.11, ci_high: 0.19 },
  ],
  r_squared_progression: [
    { factor_added: "SPY", r_squared: 0.98 },
    { factor_added: "MTUM", r_squared: 0.985 },
  ],
  variance_decomposition: [
    { name: "SPY", share: 0.9 },
    { name: "MTUM", share: 0.08 },
    { name: "idiosyncratic", share: 0.02 },
  ],
};

const MC_SPY = {
  symbol: "SPY",
  horizon: 21,
  n_paths: 10000,
  histogram: { bin_edges: [-0.1, -0.05, 0, 0.05, 0.1], counts: [10, 40, 30, 20] },
  p5: -0.08,
  p50: 0.01,
  p95: 0.12,
  es_975: 0.095,
  n_nonfinite: 0,
  fx: {
    status: "converted",
    base_currency: "USD",
    source: "ECB",
    as_of: "2026-07-24",
    fetched_at: "2026-07-24T17:00:00Z",
    missing_currencies: [],
    note: "Prices are normalized to USD with dated ECB evidence.",
  },
};

function mockHappyPath(regressionHandler?: (url: URL) => object) {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY)),
    http.get("/api/risk/:symbol/regression", ({ request }) => {
      const url = new URL(request.url);
      if (regressionHandler) return HttpResponse.json(regressionHandler(url));
      return HttpResponse.json(REGRESSION_SINGLE);
    })
  );
}

test("renders symbol picker, factor builder, and single-factor regression stats from the API", async () => {
  mockHappyPath();
  renderRisk();

  expect(await screen.findByTestId("regression-scatter")).toBeInTheDocument();
  const symbolSelect = screen.getByRole("combobox", { name: /^symbol$/i });
  expect(within(symbolSelect).getByText("SPY", { selector: "option" })).toBeInTheDocument();
  expect(within(symbolSelect).getByText("QQQ", { selector: "option" })).toBeInTheDocument();

  const primary = screen.getByRole("combobox", { name: /primary factor/i }) as HTMLSelectElement;
  await waitFor(() => expect(primary.value).toBe("SPY"));

  expect(screen.getByText(/currently analyses one symbol at a time/i)).toBeInTheDocument();
  expect(screen.getByText(/does not use the pinned book/i)).toBeInTheDocument();
  expect(screen.getAllByText(/^1\.000$/).length).toBeGreaterThan(0); // slope / beta
  expect(screen.getAllByText(/0\.980/).length).toBeGreaterThan(0); // R^2 (single + all-factor)
  expect(screen.getByText(/98\.00%/)).toBeInTheDocument(); // variance share for SPY
  expect(screen.getByText(/2\.00%/)).toBeInTheDocument(); // idiosyncratic share
});

test("surfaces separate FX evidence for risk, regression, and Monte Carlo results", async () => {
  mockHappyPath();
  server.use(http.post("/api/risk/montecarlo", () => HttpResponse.json(MC_SPY)));
  renderRisk();

  expect(await screen.findByTestId("risk-fx-evidence")).toHaveTextContent(
    "FX base USD · source ECB · as of 2026-07-22",
  );
  expect(await screen.findByTestId("regression-fx-evidence")).toHaveTextContent(
    "FX base USD · source ECB · as of 2026-07-23",
  );

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  expect(await screen.findByTestId("monte-carlo-fx-evidence")).toHaveTextContent(
    "FX base USD · source ECB · as of 2026-07-24",
  );
});

test("keeps Risk authoring controls in breakpoint-gated regions with neutral actions", async () => {
  mockHappyPath();
  renderRisk();
  await screen.findByTestId("regression-scatter");

  expect(screen.getByRole("combobox", { name: /^symbol$/i }).closest(".authoring-only")).not.toBeNull();
  expect(screen.getByRole("combobox", { name: /primary factor/i }).closest(".authoring-only-block")).not.toBeNull();
  const run = screen.getByRole("button", { name: /run monte carlo/i });
  expect(run.closest(".authoring-only")).not.toBeNull();
  expect(run).not.toHaveClass("border-you", "bg-you/10", "text-you");
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

test("adding an extra factor chip requests and renders the multi-factor regression", async () => {
  mockHappyPath((url) => {
    const factors = url.searchParams.get("factors");
    return factors === "SPY,MTUM" ? REGRESSION_MULTI : REGRESSION_SINGLE;
  });
  renderRisk();
  await screen.findByTestId("regression-scatter");

  const mtumChip = await screen.findByRole("button", { name: "MTUM", pressed: false });
  fireEvent.click(mtumChip);

  await waitFor(async () => expect((await screen.findAllByText(/0\.985/)).length).toBeGreaterThan(0));
  const betaRows = screen.getAllByRole("row");
  const mtumRow = betaRows.find((r) => within(r).queryByText("MTUM"));
  expect(mtumRow).toBeTruthy();
});

test("horizon risk panel shows the historical sqrt-t ES immediately, then MC-bootstrap ES after Run", async () => {
  mockHappyPath();
  server.use(http.post("/api/risk/montecarlo", () => HttpResponse.json(MC_SPY)));
  renderRisk();
  await screen.findByTestId("regression-scatter");

  // Historical ES = 0.0314 * sqrt(21) ~= 0.1439, computed client-side from risk60.es_975.
  await waitFor(() => expect(screen.getByText(/14\.39%/)).toBeInTheDocument());
  expect(screen.getByText(/run to see the 21-day terminal distribution/i)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  expect(await screen.findByTestId("fan-chart")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText(/12\.00%/)).toBeInTheDocument()); // p95
  expect(screen.getByText(/9\.50%/)).toBeInTheDocument(); // MC bootstrap ES
});

test("Monte Carlo reports excluded non-finite paths and the effective sample", async () => {
  mockHappyPath();
  server.use(
    http.post("/api/risk/montecarlo", () =>
      HttpResponse.json({ ...MC_SPY, n_nonfinite: 7 })
    )
  );
  renderRisk();
  await screen.findByTestId("regression-scatter");

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  const warning = await screen.findByRole("status", { name: /monte carlo sample warning/i });
  expect(warning).toHaveTextContent("7 non-finite paths were excluded");
  expect(warning).toHaveTextContent("9,993 of 10,000 requested paths");
});

test("Monte Carlo 422 surfaces the server's detail message near the controls, not a bare status", async () => {
  mockHappyPath();
  server.use(
    http.post("/api/risk/montecarlo", () =>
      HttpResponse.json(
        { detail: "simulation produced no finite terminal returns — check cached bars for zero/degenerate prices" },
        { status: 422 }
      )
    )
  );
  renderRisk();
  await screen.findByTestId("regression-scatter");

  fireEvent.click(screen.getByRole("button", { name: /run monte carlo/i }));

  expect(await screen.findByText(/no finite terminal returns/i)).toBeInTheDocument();
  expect(screen.queryByText(/→ 422/)).not.toBeInTheDocument();
});

test("Risk series 422 surfaces the server's detail message near the beta panel", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () =>
      HttpResponse.json({ detail: "symbol 'SPY' has no cached bars" }, { status: 422 })
    ),
    http.get("/api/risk/:symbol/regression", () =>
      HttpResponse.json({ detail: "symbol 'SPY' has no cached bars" }, { status: 422 })
    )
  );
  renderRisk();
  expect(await screen.findByText(/no cached bars/i)).toBeInTheDocument();
  expect(screen.queryByText(/→ 422/)).not.toBeInTheDocument();
});

test("Regression 422 surfaces the server's detail message near the regression panel", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/risk/:symbol", () => HttpResponse.json(RISK_SPY)),
    http.get("/api/risk/:symbol/regression", () =>
      HttpResponse.json({ detail: "only 12 overlapping observations; need >= 30" }, { status: 422 })
    )
  );
  renderRisk();
  expect(await screen.findByText(/need >= 30/i)).toBeInTheDocument();
  expect(screen.queryByText(/→ 422/)).not.toBeInTheDocument();
});

test("years/regression-window/horizon/paths controls are bounded matching backend limits", async () => {
  mockHappyPath();
  renderRisk();
  await screen.findByTestId("regression-scatter");

  const yearsInput = screen.getByLabelText(/^years/i) as HTMLInputElement;
  expect(yearsInput.min).toBe("1");
  expect(yearsInput.max).toBe("25");

  fireEvent.click(screen.getByRole("checkbox", { name: /trim to window/i }));
  const regWindowInput = await screen.findByLabelText(/regression window/i) as HTMLInputElement;
  expect(regWindowInput.min).toBe("20");
  expect(regWindowInput.max).toBe("2520");

  const horizonInput = screen.getByLabelText(/^horizon/i) as HTMLInputElement;
  expect(horizonInput.min).toBe("1");
  expect(horizonInput.max).toBe("2520");

  const pathsInput = screen.getByLabelText(/^paths/i) as HTMLInputElement;
  expect(pathsInput.min).toBe("1");
  expect(pathsInput.max).toBe("200000");
});

test("Risk page surfaces the volatility-drag and skill-vs-luck (alpha honesty) lenses", async () => {
  mockHappyPath();
  renderRisk();
  await screen.findByTestId("regression-scatter");

  // Volatility drag: CAGR (geometric) distinct from arithmetic mean, plus the equity-sleeve note.
  expect(await screen.findByText(/18\.50%/)).toBeInTheDocument(); // CAGR
  expect(screen.getByText(/equity sleeve/i)).toBeInTheDocument();

  // Alpha honesty: t-stat / IR and the honest excess-return note; |t|>=2 => distinguishable from luck.
  expect(screen.getByText(/2\.00 \/ 1\.50/)).toBeInTheDocument();
  expect(screen.getByText(/rf=US3M\/252/)).toBeInTheDocument();
  expect(screen.getByText(/statistically distinguishable from luck/i)).toBeInTheDocument();
});

test("Risk page explains unavailable alpha without making a skill-vs-luck claim", async () => {
  const unavailable = {
    ...REGRESSION_SINGLE,
    fit_line: { ...REGRESSION_SINGLE.fit_line, intercept: null },
    alpha_daily: null,
    alpha_annualized: null,
    alpha_se: null,
    alpha_ci: [null, null],
    alpha_tstat: null,
    information_ratio: null,
    alpha_note: "alpha unavailable: US3M risk-free series is not cached",
    attribution: REGRESSION_SINGLE.attribution.map((row) =>
      row.name === "alpha" ? { ...row, daily: null, annualized: null } : row
    ),
  };
  mockHappyPath(() => unavailable);
  renderRisk();

  expect(await screen.findByText(/alpha unavailable: US3M/i)).toBeInTheDocument();
  expect(screen.queryByText(/alpha not distinguishable from luck/i)).not.toBeInTheDocument();
  expect(screen.getByText(/skill-vs-luck unavailable/i)).toBeInTheDocument();
});
