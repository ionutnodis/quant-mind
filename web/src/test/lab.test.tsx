/**
 * Lab bench page tests (Task 3, parallel-pages plan): schema-driven model
 * form renders; Fit surfaces diagnostics/CIs; Apply to Book renders the
 * amber P&L results panel. Plotly needs real canvas — LabFanChart is
 * stubbed in jsdom, same pattern as today.test.tsx stubs CorrelationHeatmap.
 * No @testing-library/user-event dependency in this repo — fireEvent only.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { Lab } from "../pages/Lab";

vi.mock("../components/LabFanChart", () => ({
  LabFanChart: () => <div data-testid="lab-fan-chart" />,
  PairBandsChart: () => <div data-testid="pair-bands-chart" />,
}));

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderLab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Lab />
    </QueryClientProvider>
  );
}

const MODEL_SCHEMA = {
  name: "ou",
  label: "Ornstein-Uhlenbeck (mean-reverting)",
  factor: { kind: "rate_level", units: "decimal", dt: 1 / 252 },
  params: {
    theta: { label: "θ (mean-reversion speed, /yr)", type: "float" },
    mu: { label: "μ (long-run level)", type: "float" },
    sigma: { label: "σ (volatility, /√yr)", type: "float" },
  },
};

const FIT_RESPONSE = {
  model_name: "ou",
  params: { theta: 1.2345, mu: 0.0421, sigma: 0.0187 },
  cis: {
    theta: [1.0, 1.5],
    mu: [0.04, 0.045],
    sigma: [0.015, 0.021],
  },
  diagnostics: { adf_pvalue: 0.012, aic: -123.4, log_likelihood: 65.2, r_squared: 0.87 },
  n_obs: 252,
};

const SIMULATE_RESPONSE = {
  bands: {
    p5: [0.04, 0.041],
    p25: [0.041, 0.042],
    p50: [0.042, 0.043],
    p75: [0.043, 0.044],
    p95: [0.044, 0.045],
  },
  sample_paths: [[0.042, 0.0425]],
  horizon: 2,
  n_paths: 500,
};

const APPLY_RESPONSE = {
  histogram: { bin_edges: [-100, 0, 100], counts: [3, 2] },
  mean: -450.0,
  p5: -6100.0,
  p50: -300.0,
  p95: 5200.0,
  es: -6800.0,
  horizon: 60,
  n_paths: 500,
  n_nonfinite: 0,
};

// Wave-3B practitioner fixtures: every OU fit now carries the derived
// half-life/displacement/random-walk-gate diagnostics.
const FIT_DERIVED = {
  ...FIT_RESPONSE,
  diagnostics: {
    ...FIT_RESPONSE.diagnostics,
    x_last: 0.0512,
    half_life_days: 34.2,
    half_life_ci_lo: 21.0,
    half_life_ci_hi: 61.0,
    displacement_sigma: 2.1,
    stationary_sigma: 0.005,
    mean_reversion: 1,
    delta_aic: 6.2,
    lr_stat: 8.2,
    aic_rw: -117.2,
  },
};

const BOOK_SNAPSHOT = {
  snapshot_id: "abc123def456",
  valuation_ts: "2026-07-25T00:00:00Z",
  base_currency: "USD",
  positions: [],
};

const BOOK_REG_RESPONSE = {
  factor_series: "US10Y",
  horizon: "daily",
  exposure_units: "usd_per_bp",
  beta_usd_per_bp: -612.4,
  beta_se: 45.1,
  beta_ci: [-701.2, -523.6],
  alpha_usd: 1.2,
  alpha_se: 0.8,
  r_squared: 0.41,
  n_obs: 1250,
  hac_lags: 7,
  book_gross: 812345.0,
  as_of: "2026-07-24T00:00:00Z",
};

const PAIR_RESPONSE = {
  y_symbol: "BBB",
  x_symbol: "AAA",
  horizon: "daily",
  coint_pvalue: 0.003,
  hedge_ratio: 0.5123,
  hedge_ratio_se: 0.012,
  is_cointegrated: true,
  dates: ["2026-07-23T00:00:00Z", "2026-07-24T00:00:00Z"],
  spread: [1.2, 1.4],
  mu: 1.1,
  stationary_sigma: 0.2,
  current_z: 2.4,
  half_life_days: 34.2,
  half_life_ci: [21.0, 61.0],
  mean_reversion_established: true,
  fit: FIT_DERIVED,
  n_obs: 252,
  as_of: "2026-07-24T00:00:00Z",
};

test("schema-driven model form renders from GET /api/models", async () => {
  server.use(http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])));
  renderLab();
  expect(await screen.findByText(/Ornstein-Uhlenbeck/)).toBeInTheDocument();
  expect(screen.getByLabelText(/symbol/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/years/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^fit$/i })).toBeInTheDocument();
  // Structured empty states before any fit — never a crash, never fake data.
  expect(screen.getAllByText(/awaiting fit/i).length).toBeGreaterThan(0);
});

test("fit surfaces parameter estimates with CIs and diagnostics", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_RESPONSE))
  );
  renderLab();
  const fitButton = await screen.findByRole("button", { name: /^fit$/i });
  fireEvent.click(fitButton);

  expect(await screen.findByText(/θ/)).toBeInTheDocument();
  expect(screen.getByText(/1\.2345/)).toBeInTheDocument();
  expect(screen.getByText(/1\.0000/)).toBeInTheDocument(); // CI lower bound rendered
  expect(screen.getByText(/ADF/i)).toBeInTheDocument();
  expect(screen.getByText(/0\.0120/)).toBeInTheDocument();
  expect(screen.getByText(/AIC/i)).toBeInTheDocument();
  expect(screen.getByText(/R²/)).toBeInTheDocument();
});

test("simulate renders the fan chart once a fit exists", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_RESPONSE)),
    http.post("/api/models/ou/simulate", () => HttpResponse.json(SIMULATE_RESPONSE))
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  await screen.findByText(/θ/);

  const simulateButton = await screen.findByRole("button", { name: /^simulate$/i });
  expect(simulateButton).toBeEnabled();
  fireEvent.click(simulateButton);
  expect(await screen.findByTestId("lab-fan-chart")).toBeInTheDocument();
});

test("apply to book renders the amber P&L results panel", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_RESPONSE)),
    http.post("/api/lab/apply", () => HttpResponse.json(APPLY_RESPONSE))
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  await screen.findByText(/θ/);

  const valueInput = screen.getByLabelText(/exposure value/i);
  fireEvent.change(valueInput, { target: { value: "-610" } });

  const applyButton = screen.getByRole("button", { name: /^apply$/i });
  expect(applyButton).toBeEnabled();
  fireEvent.click(applyButton);

  const results = await screen.findByTestId("apply-results");
  expect(results).toHaveClass("text-you");
  expect(within(results).getByText(/-6,?800\.00/)).toBeInTheDocument();
  await waitFor(() => expect(within(results).getByText(/es/i)).toBeInTheDocument());
});

test("apply is honest when some paths were dropped as non-finite", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_RESPONSE)),
    http.post("/api/lab/apply", () =>
      HttpResponse.json({ ...APPLY_RESPONSE, n_nonfinite: 12 })
    )
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  await screen.findByText(/θ/);
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
  expect(await screen.findByText(/12 paths produced non-finite/i)).toBeInTheDocument();
});

test("apply is disabled and honest about the awaiting state before a fit exists", async () => {
  server.use(http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])));
  renderLab();
  await screen.findByText(/Ornstein-Uhlenbeck/);
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
});

// --- wave-3B practitioner tests ---

test("fit renders the half-life + displacement readout with its CI", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_DERIVED))
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  expect(await screen.findByText(/2\.1σ above mean/)).toBeInTheDocument();
  expect(screen.getByText(/half-life 34d/)).toBeInTheDocument();
  expect(screen.getByText(/95% CI 21–61d/)).toBeInTheDocument();
});

test("random-walk gate labels mean reversion not established", async () => {
  const rwFit = {
    ...FIT_DERIVED,
    diagnostics: { ...FIT_DERIVED.diagnostics, mean_reversion: 0, delta_aic: -1.4, lr_stat: 0.6 },
  };
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(rwFit))
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  expect(await screen.findByText(/mean reversion not established/i)).toBeInTheDocument();
});

test("book exposure regression renders the amber sensitivity with SE/CI and daily horizon", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.get("/api/book/current", () => HttpResponse.json(BOOK_SNAPSHOT)),
    http.post("/api/lab/book-regression", () => HttpResponse.json(BOOK_REG_RESPONSE))
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /regress/i }));
  const results = await screen.findByTestId("book-regression-results");
  expect(results).toHaveClass("text-you"); // book quantity → amber, the law
  expect(within(results).getByText(/-612/)).toBeInTheDocument();
  expect(within(results).getByText(/±\s*45/)).toBeInTheDocument(); // SE displayed
  expect(within(results).getByText(/-701/)).toBeInTheDocument(); // CI lower bound
  expect(within(results).getByText(/daily horizon/i)).toBeInTheDocument(); // horizon label
});

test("one-click Use in Apply feeds the regression beta into apply-to-book", async () => {
  // Holder object (not a `X | null` let): TS control-flow analysis doesn't
  // track assignments inside the msw handler closure and would narrow a
  // null-initialized variable to `never` at the assertion site.
  const captured: { applyBody?: { exposure?: { units?: string; value?: number } } } = {};
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.get("/api/book/current", () => HttpResponse.json(BOOK_SNAPSHOT)),
    http.post("/api/lab/book-regression", () => HttpResponse.json(BOOK_REG_RESPONSE)),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_DERIVED)),
    http.post("/api/lab/apply", async ({ request }) => {
      captured.applyBody = (await request.json()) as typeof captured.applyBody;
      return HttpResponse.json(APPLY_RESPONSE);
    })
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  await screen.findByText(/θ/);
  fireEvent.click(screen.getByRole("button", { name: /regress/i }));
  await screen.findByTestId("book-regression-results");

  fireEvent.click(screen.getByRole("button", { name: /use in apply/i }));
  await screen.findByTestId("apply-results");
  expect(captured.applyBody?.exposure?.units).toBe("usd_per_bp");
  expect(captured.applyBody?.exposure?.value).toBe(-612.4);
});

test("pair pipeline renders the EG→OU readout with the z-bands chart", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/lab/pair", () => HttpResponse.json(PAIR_RESPONSE))
  );
  renderLab();
  await screen.findByText(/Ornstein-Uhlenbeck/);
  fireEvent.click(screen.getByRole("button", { name: /run pair/i }));
  expect(await screen.findByTestId("pair-bands-chart")).toBeInTheDocument();
  expect(screen.getByText(/0\.5123/)).toBeInTheDocument(); // hedge ratio
  expect(screen.getByText(/0\.0030/)).toBeInTheDocument(); // EG p-value
  expect(screen.getByText(/2\.4σ above mean/)).toBeInTheDocument(); // displacement
  expect(screen.getByText(/half-life 34d/)).toBeInTheDocument();
});

test("pair pipeline is honest when the pair is not cointegrated and RW wins", async () => {
  const honest = {
    ...PAIR_RESPONSE,
    coint_pvalue: 0.41,
    is_cointegrated: false,
    mean_reversion_established: false,
    current_z: null,
    half_life_days: null,
    half_life_ci: null,
    stationary_sigma: null,
  };
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/lab/pair", () => HttpResponse.json(honest))
  );
  renderLab();
  await screen.findByText(/Ornstein-Uhlenbeck/);
  fireEvent.click(screen.getByRole("button", { name: /run pair/i }));
  expect(await screen.findByText(/not cointegrated/i)).toBeInTheDocument();
  expect(screen.getByText(/mean reversion not established/i)).toBeInTheDocument();
});

test("unsupported exposure mapping surfaces the backend's refusing message, not a crash", async () => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])),
    http.post("/api/models/ou/fit", () => HttpResponse.json(FIT_RESPONSE)),
    http.post("/api/lab/apply", () =>
      HttpResponse.json(
        {
          detail:
            "exposure is for factor kind 'vol_points' but model simulates 'rate_level' — refusing to produce a dimensionally wrong number",
        },
        { status: 422 }
      )
    )
  );
  renderLab();
  fireEvent.click(await screen.findByRole("button", { name: /^fit$/i }));
  await screen.findByText(/θ/);
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
  expect(await screen.findByText(/refusing/i)).toBeInTheDocument();
});

test("Book Exposure panel names the resolved pinned ref (batch-2 item 5)", async () => {
  // "Uses ?book_ref= if pinned" alone left the user guessing WHICH book the
  // regression would hit — the resolved ref must be displayed.
  window.history.replaceState(null, "", "/?book_ref=snap-abc123");
  server.use(http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])));
  renderLab();
  await screen.findByText(/Ornstein-Uhlenbeck/);
  expect(screen.getByText(/snap-abc123/)).toBeInTheDocument();
  window.history.replaceState(null, "", "/");
});
