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
  histogram: { edges: [-100, 0, 100], counts: [3, 2] },
  mean: -450.0,
  p5: -6100.0,
  p50: -300.0,
  p95: 5200.0,
  es: -6800.0,
  horizon: 60,
  n_paths: 500,
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

test("apply is disabled and honest about the awaiting state before a fit exists", async () => {
  server.use(http.get("/api/models", () => HttpResponse.json([MODEL_SCHEMA])));
  renderLab();
  await screen.findByText(/Ornstein-Uhlenbeck/);
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
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
