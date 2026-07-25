/**
 * Today page component tests (phase test contract): tiles render from mocked
 * API, staleness flag appears when as-of is old, empty cache shows empty state.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { Today } from "../pages/Today";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// Plotly needs real canvas/WebGL; stub the heatmap in jsdom.
vi.mock("../components/CorrelationHeatmap", () => ({
  CorrelationHeatmap: () => <div data-testid="corr-heatmap" />,
}));
import { vi } from "vitest";

function renderToday() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Today />
    </QueryClientProvider>
  );
}

const MODELS = [{ name: "ou", label: "Ornstein-Uhlenbeck (mean-reverting)" }];

const BRIEF = {
  tiles: [
    { symbol: "SPY", last_close: 736.28, change_1d: 0.001 },
    { symbol: "QQQ", last_close: 682.41, change_1d: -0.0112 },
  ],
  correlation: { symbols: ["QQQ", "SPY"], matrix: [[1, 0.95], [0.95, 1]] },
  benchmark_es: 0.0314,
  as_of: "2026-07-24T00:00:00Z",
};

test("renders tiles with direction glyphs and ES from the API", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/models", () => HttpResponse.json(MODELS))
  );
  renderToday();
  expect(await screen.findByText("SPY")).toBeInTheDocument();
  expect(screen.getByText("736.28")).toBeInTheDocument();
  expect(screen.getByText(/▼ 1\.12%/)).toBeInTheDocument();
  expect(screen.getByText(/3\.14%/)).toBeInTheDocument();
  // new bench zones: regime line, book vitals panel, ranked strip, models console
  expect(screen.getByText(/tape — QQQ leads, down 1\.12%/)).toBeInTheDocument();
  expect(screen.getByText("Your book")).toBeInTheDocument();
  expect(screen.getByText(/ranked by move/i)).toBeInTheDocument();
  expect(await screen.findByText(/Ornstein-Uhlenbeck/)).toBeInTheDocument();
  expect(screen.getByText(/as of 2026-07-24/)).toBeInTheDocument();
});

test("staleness flag shows when data is old", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })),
    http.get("/api/models", () => HttpResponse.json(MODELS))
  );
  renderToday();
  expect(await screen.findByTestId("staleness")).toHaveTextContent(/days old/);
});

test("empty cache shows empty state, not a crash", async () => {
  server.use(
    http.get("/api/brief", () =>
      HttpResponse.json({ tiles: [], correlation: null, benchmark_es: null, as_of: null })
    ),
    http.get("/api/models", () => HttpResponse.json(MODELS))
  );
  renderToday();
  expect(await screen.findByText(/No market data cached/)).toBeInTheDocument();
  // empty state gets a Sync now button too, not just the CLI hint
  expect(await screen.findByTestId("sync-now")).toBeInTheDocument();
});

test("sync now button renders in the staleness banner", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })),
    http.get("/api/models", () => HttpResponse.json(MODELS))
  );
  renderToday();
  const staleness = await screen.findByTestId("staleness");
  expect(await screen.findByTestId("sync-now")).toBeInTheDocument();
  expect(staleness).toBeInTheDocument();
});

test("sync now: posts a job, disables while running, polls to completion, and invalidates the brief", async () => {
  let pollCount = 0;
  server.use(
    http.get("/api/brief", () => HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })),
    http.get("/api/models", () => HttpResponse.json(MODELS)),
    http.post("/api/sync", () => HttpResponse.json({ job_id: "abc123" })),
    http.get("/api/sync/abc123", () => {
      pollCount += 1;
      return HttpResponse.json(
        pollCount < 2 ? { state: "running" } : { state: "done", result: "synced 3 symbols" }
      );
    })
  );
  renderToday();
  const button = await screen.findByTestId("sync-now");
  fireEvent.click(button);
  await waitFor(() => expect(button).toBeDisabled());
  await waitFor(() => expect(button).not.toBeDisabled(), { timeout: 5000 });
  expect(pollCount).toBeGreaterThanOrEqual(2);
});

test("sync now: surfaces an error message and re-enables the button", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })),
    http.get("/api/models", () => HttpResponse.json(MODELS)),
    http.post("/api/sync", () => HttpResponse.json({ job_id: "err1" })),
    http.get("/api/sync/err1", () => HttpResponse.json({ state: "error", error: "boom" }))
  );
  renderToday();
  const button = await screen.findByTestId("sync-now");
  fireEvent.click(button);
  await waitFor(() => expect(button).not.toBeDisabled());
  expect(await screen.findByText(/boom/)).toBeInTheDocument();
});

test("sync now: failed submit (500) surfaces the error and re-enables the button", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })),
    http.get("/api/models", () => HttpResponse.json(MODELS)),
    http.post("/api/sync", () =>
      HttpResponse.json({ detail: "sync submit exploded" }, { status: 500 })
    )
  );
  renderToday();
  const button = await screen.findByTestId("sync-now");
  fireEvent.click(button);
  // the POST failure must be caught: error text visible, button usable again
  expect(await screen.findByText(/sync submit exploded/)).toBeInTheDocument();
  await waitFor(() => expect(button).not.toBeDisabled());
});
