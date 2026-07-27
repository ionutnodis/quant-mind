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

// Default: an empty book — individual tests override with PORTFOLIO_LIVE.
// Registered at setup (not per-test) because resetHandlers() restores these
// and every Today render now fetches /api/portfolio for the vitals panel.
const EMPTY_PORTFOLIO = {
  valuation_ts: "2026-07-27T11:00:00Z",
  totals: { market_value: 0, n_positions: 0, unrealized_pnl: null },
  attribution: { available: false, beta: null, window_days: null },
  options_sleeve: { available: false, reason: null },
};

const server = setupServer(
  http.get("/api/portfolio", () => HttpResponse.json(EMPTY_PORTFOLIO))
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

import { vi } from "vitest";

// Plotly needs real canvas/WebGL; stub the rotation heatmap in jsdom. Each
// of these three components owns its own data fetch (POST /api/rotation,
// GET /api/news, GET /api/instruments/*/candles + /api/macro) — they get
// their own dedicated component tests (rotationheatmap.test.tsx,
// newsticker.test.tsx, glancecharts.test.tsx); Today's own test stays
// scoped to what /api/brief + /api/models drive.
vi.mock("../components/RotationHeatmap", async () => {
  const { useEffect } = await import("react");
  return {
    // The stub reports an as-of like the real component so Today's Rotation
    // panel note (F7 as-of stamp) is exercised here.
    RotationHeatmap: ({ onAsOf }: { onAsOf?: (asOf: string | null) => void }) => {
      useEffect(() => {
        onAsOf?.("2026-07-24T00:00:00Z");
      }, [onAsOf]);
      return <div data-testid="rotation-heatmap-stub" />;
    },
  };
});
vi.mock("../components/NewsTicker", () => ({
  NewsTicker: () => <div data-testid="news-ticker-stub" />,
}));
vi.mock("../components/GlanceCharts", () => ({
  GlanceCharts: () => <div data-testid="glance-charts-stub" />,
}));

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
  expect(screen.getAllByText(/as of 2026-07-24/).length).toBeGreaterThan(0);
  // Rotation panel note carries the rotation data's own as-of (F7)
  expect(
    await screen.findByText(/as of 2026-07-24 · click a mover for the other side of the trade/)
  ).toBeInTheDocument();
  // wave-3B additions are wired into the page (each component's behavior is
  // covered by its own dedicated test file — here just prove Today mounts them)
  expect(screen.getByTestId("news-ticker-stub")).toBeInTheDocument();
  expect(screen.getByTestId("glance-charts-stub")).toBeInTheDocument();
  expect(screen.getByTestId("rotation-heatmap-stub")).toBeInTheDocument();
});

test("book vitals light up amber from the live portfolio", async () => {
  // 2026-07-27: the real account connected but the panel still said "No
  // positions yet" — the vitals were a static placeholder, never wired.
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/models", () => HttpResponse.json(MODELS)),
    http.get("/api/portfolio", () =>
      HttpResponse.json({
        valuation_ts: "2026-07-27T11:24:06Z",
        totals: { market_value: 48721.48, n_positions: 9, unrealized_pnl: 195.2938 },
        attribution: { available: true, beta: 0.4820764, window_days: 90 },
        options_sleeve: { available: false, reason: "chain not ingested — run options_sync" },
      })
    )
  );
  renderToday();
  const pnl = await screen.findByTestId("vital-pnl");
  expect(pnl).toHaveTextContent("+$195.29");
  expect(pnl).toHaveClass("text-you"); // book quantity — amber law
  const beta = screen.getByTestId("vital-beta");
  expect(beta).toHaveTextContent("0.48");
  expect(beta).toHaveClass("text-you");
  // beta labeled with the window it was actually computed over (90d), never
  // a hardcoded convention
  expect(screen.getByText(/Beta \(90d\)/)).toBeInTheDocument();
  expect(screen.getByText(/9 positions/)).toBeInTheDocument();
  expect(screen.queryByText(/No positions yet/)).not.toBeInTheDocument();
});

test("book vitals keep the honest empty state when no positions exist", async () => {
  server.use(
    http.get("/api/brief", () => HttpResponse.json(BRIEF)),
    http.get("/api/models", () => HttpResponse.json(MODELS))
  );
  renderToday();
  expect(await screen.findByText(/No positions yet/)).toBeInTheDocument();
  expect(screen.getByTestId("vital-pnl")).toHaveTextContent("—");
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
