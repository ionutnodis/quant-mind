/**
 * Today page component tests (phase test contract): tiles render from mocked
 * API, staleness flag appears when as-of is old, empty cache shows empty state.
 */
import { render, screen } from "@testing-library/react";
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
  server.use(http.get("/api/brief", () => HttpResponse.json(BRIEF)));
  renderToday();
  expect(await screen.findByText("SPY")).toBeInTheDocument();
  expect(screen.getByText("736.28")).toBeInTheDocument();
  expect(screen.getByText(/▼ 1\.12%/)).toBeInTheDocument();
  expect(screen.getByText(/3\.14%/)).toBeInTheDocument();
  expect(screen.getByTestId("asof")).toHaveTextContent("2026-07-24");
});

test("staleness flag shows when data is old", async () => {
  server.use(
    http.get("/api/brief", () =>
      HttpResponse.json({ ...BRIEF, as_of: "2026-07-01T00:00:00Z" })
    )
  );
  renderToday();
  expect(await screen.findByTestId("staleness")).toHaveTextContent(/days old/);
});

test("empty cache shows empty state, not a crash", async () => {
  server.use(
    http.get("/api/brief", () =>
      HttpResponse.json({ tiles: [], correlation: null, benchmark_es: null, as_of: null })
    )
  );
  renderToday();
  expect(await screen.findByText(/Cache is empty/)).toBeInTheDocument();
});
