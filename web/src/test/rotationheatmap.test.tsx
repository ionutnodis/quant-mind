/**
 * RotationHeatmap tests (wave-3B Today task): universe/window/lookback
 * pickers drive POST /api/rotation, the heatmap renders in the backend's
 * clustered order (delegated to the stubbed CorrelationHeatmap — Plotly
 * needs real canvas/WebGL), and clicking a symbol's return badge enters
 * "other side of the trade" mode with the anchor-scored ranking.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { RotationHeatmap } from "../components/RotationHeatmap";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

vi.mock("../components/InstrumentHover", () => ({
  InstrumentHover: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));
vi.mock("../components/CorrelationHeatmap", () => ({
  CorrelationHeatmap: ({ data }: { data: { symbols: string[] } }) => (
    <div data-testid="corr-heatmap-stub">{data.symbols.join(",")}</div>
  ),
}));

function renderRotation() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RotationHeatmap />
    </QueryClientProvider>
  );
}

const BASE_RESPONSE = {
  universe: "sectors",
  symbols: ["XLK", "XLF"],
  matrix: [
    [1.0, 0.5],
    [0.5, 1.0],
  ],
  corr_window: 60,
  return_days: 5,
  returns: [
    { symbol: "XLK", ret: 0.02 },
    { symbol: "XLF", ret: -0.015 },
  ],
  anchor: null,
  other_side: null,
  as_of: "2026-07-26T00:00:00Z",
  missing: [],
};

const OTHER_SIDE_RESPONSE = {
  ...BASE_RESPONSE,
  anchor: "XLF",
  other_side: [
    { symbol: "XLK", corr: -0.4, ret: 0.02, score: 0.008 },
  ],
};

test("renders the clustered heatmap and symbol return strip from the default (sectors) universe", async () => {
  let lastBody: unknown = null;
  server.use(
    http.post("/api/rotation", async ({ request }) => {
      lastBody = await request.json();
      return HttpResponse.json(BASE_RESPONSE);
    })
  );
  renderRotation();

  expect(await screen.findByTestId("corr-heatmap-stub")).toHaveTextContent("XLK,XLF");
  expect(screen.getByTestId("rotation-symbol-XLK")).toHaveTextContent("2.00%");
  expect(screen.getByTestId("rotation-symbol-XLF")).toHaveTextContent("1.50%");
  expect(lastBody).toMatchObject({ universe: "sectors", corr_window: 60, return_days: 5 });
});

test("changing universe/corr-window/return-days re-requests with the new params", async () => {
  const bodies: unknown[] = [];
  server.use(
    http.post("/api/rotation", async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(BASE_RESPONSE);
    })
  );
  renderRotation();
  await screen.findByTestId("corr-heatmap-stub");

  fireEvent.change(screen.getByTestId("rotation-universe"), { target: { value: "factors" } });
  await waitFor(() => expect(bodies.some((b) => (b as { universe: string }).universe === "factors")).toBe(true));

  fireEvent.change(screen.getByTestId("rotation-corr-window"), { target: { value: "120" } });
  await waitFor(() =>
    expect(bodies.some((b) => (b as { corr_window: number }).corr_window === 120)).toBe(true)
  );

  fireEvent.change(screen.getByTestId("rotation-return-days"), { target: { value: "21" } });
  await waitFor(() =>
    expect(bodies.some((b) => (b as { return_days: number }).return_days === 21)).toBe(true)
  );
});

test("custom universe sends the typed comma-separated symbol list", async () => {
  const bodies: unknown[] = [];
  server.use(
    http.post("/api/rotation", async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json({ ...BASE_RESPONSE, universe: "custom" });
    })
  );
  renderRotation();
  fireEvent.change(screen.getByTestId("rotation-universe"), { target: { value: "custom" } });
  const input = await screen.findByTestId("rotation-custom-symbols");
  fireEvent.change(input, { target: { value: "spy, qqq ,gld" } });

  await waitFor(() =>
    expect(bodies.some((b) => JSON.stringify((b as { symbols?: string[] }).symbols) === JSON.stringify(["SPY", "QQQ", "GLD"]))).toBe(true)
  );
});

test("clicking a symbol's return sets it as anchor and shows the other-side ranking", async () => {
  server.use(
    http.post("/api/rotation", async ({ request }) => {
      const body = (await request.json()) as { anchor?: string };
      return HttpResponse.json(body.anchor ? OTHER_SIDE_RESPONSE : BASE_RESPONSE);
    })
  );
  renderRotation();
  await screen.findByTestId("corr-heatmap-stub");

  fireEvent.click(screen.getByTestId("rotation-symbol-XLF"));

  const otherSide = await screen.findByTestId("rotation-other-side");
  expect(otherSide).toHaveTextContent(/Other side of XLF/);
  expect(screen.getByTestId("rotation-other-side-XLK")).toHaveTextContent("2.00%");
  expect(screen.getByTestId("rotation-clear-anchor")).toBeInTheDocument();

  // Amber law (CLAUDE.md): the anchor highlight is market data, never the
  // book — steel/neutral selection only, `you` classes must never appear.
  const anchorButton = screen.getByTestId("rotation-symbol-XLF");
  expect(anchorButton.className).not.toMatch(/\btext-you\b/);
  expect(anchorButton.className).toMatch(/\btext-ink\b/);
  const anchorChip = anchorButton.parentElement!;
  expect(anchorChip.className).not.toMatch(/\bborder-you\b/);
  expect(anchorChip.className).toMatch(/\bborder-market\b/);

  fireEvent.click(screen.getByTestId("rotation-clear-anchor"));
  await waitFor(() => expect(screen.queryByTestId("rotation-other-side")).not.toBeInTheDocument());
});

test("honest empty state when the universe has no cached data", async () => {
  server.use(
    http.post("/api/rotation", () =>
      HttpResponse.json({ ...BASE_RESPONSE, symbols: [], matrix: [], returns: [], missing: ["XLK", "XLF"] })
    )
  );
  renderRotation();
  expect(await screen.findByText(/No cached data for this universe/)).toHaveTextContent(
    /missing: XLK, XLF/
  );
});

test("422 from the backend surfaces as an error, not a crash", async () => {
  server.use(
    http.post("/api/rotation", () => HttpResponse.json({ detail: "unknown symbol(s): ['GHOST']" }, { status: 422 }))
  );
  renderRotation();
  fireEvent.change(screen.getByTestId("rotation-universe"), { target: { value: "custom" } });
  fireEvent.change(await screen.findByTestId("rotation-custom-symbols"), { target: { value: "GHOST" } });
  expect(await screen.findByText(/Rotation unavailable/)).toHaveTextContent(/GHOST/);
});
