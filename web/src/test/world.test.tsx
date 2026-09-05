import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { World } from "../pages/World";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.history.replaceState(null, "", "/world");
});
afterAll(() => server.close());

const response = {
  items: [
    { id: "energy", source_id: "reuters", source_name: "Reuters", title: "Oil supply talks resume", url: "https://example.com/oil", summary: "Ministers meet in Vienna.", published_at: "2026-09-05T08:15:00Z", time_kind: "published", topics: ["Energy"], regions: ["Europe"], relevance: 0.9, reasons: ["Matches interest: energy", "Mentions BP"], matched_symbols: ["BP"] },
    { id: "rates", source_id: "ecb", source_name: "ECB", title: "ECB publishes rate decision", url: "https://example.com/rates", summary: "The Governing Council held rates.", published_at: "2026-09-05T09:00:00Z", time_kind: "observed", topics: ["Rates"], regions: ["Europe"], relevance: 0, reasons: [], matched_symbols: [] },
  ],
  sources: [
    { id: "reuters", name: "Reuters", category: "News", homepage: "https://reuters.com", access: "public", description: "Headlines", enabled: true, state: "ok", last_attempt: "2026-09-05T09:00:00Z", last_success: "2026-09-05T09:00:00Z", next_refresh: null, item_count: 12, error: null, stale: false },
    { id: "x", name: "X", category: "Social", homepage: "javascript:alert(1)", access: "API setup required", description: "Optional social source", enabled: false, state: "disabled", last_attempt: null, last_success: null, next_refresh: null, item_count: 0, error: null, stale: false },
  ],
  profile: { watch_symbols: ["BP"], interests: ["energy"], regions: ["Europe"] },
  context: { book_ref: null, symbols: [], label: "Personal lens" },
  as_of: "2026-09-05T09:00:00Z",
  refreshing: false,
};

function renderWorld() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><World /></QueryClientProvider>);
}

test("filters cached events and keeps explicit relevance reasons", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  expect(await screen.findByText("Oil supply talks resume")).toBeInTheDocument();
  expect(screen.getByText("Matches interest: energy")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "rate decision" } });
  expect(screen.queryByText("Oil supply talks resume")).not.toBeInTheDocument();
  expect(screen.getByText("ECB publishes rate decision")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /my lens/i }));
  expect(screen.getByText(/no events match/i)).toBeInTheDocument();
});

test("saves the edited profile and renders the server-normalized response", async () => {
  let received: unknown;
  let reads = 0;
  server.use(
    http.get("/api/world", () => HttpResponse.json(reads++ ? { ...response, profile: { watch_symbols: ["NVDA", "BP"], interests: ["chips"], regions: ["US"] }, items: [{ ...response.items[0], id: "chips", title: "Chip lens reranked" }] } : response)),
    http.put("/api/world/profile", async ({ request }) => {
      received = await request.json();
      return HttpResponse.json({ watch_symbols: ["NVDA", "BP"], interests: ["chips"], regions: ["US"] });
    }),
  );
  renderWorld();
  fireEvent.change(await screen.findByLabelText(/watch symbols/i), { target: { value: "nvda, bp" } });
  fireEvent.change(screen.getByLabelText(/^interests/i), { target: { value: "chips" } });
  fireEvent.change(screen.getByLabelText(/^regions/i), { target: { value: "US" } });
  fireEvent.click(screen.getByRole("button", { name: /save lens/i }));
  expect(await screen.findByText(/lens saved/i)).toBeInTheDocument();
  expect(received).toEqual({ watch_symbols: ["NVDA", "BP"], interests: ["chips"], regions: ["US"] });
  expect(screen.getByLabelText(/watch symbols/i)).toHaveValue("NVDA, BP");
  expect(await screen.findByText("Chip lens reranked")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^interests/i), { target: { value: "unsaved interest" } });
  expect(screen.queryByText(/lens saved/i)).not.toBeInTheDocument();
});

test("keeps raw comma-separated input intact while typing a second symbol", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  const input = await screen.findByLabelText(/watch symbols/i);
  fireEvent.change(input, { target: { value: "" } });
  for (const character of "NVDA, ASML") {
    fireEvent.input(input, { target: { value: `${(input as HTMLInputElement).value}${character}` } });
  }
  expect(input).toHaveValue("NVDA, ASML");
});

test("keeps lens fields locked while the submitted profile is being saved", async () => {
  let finishSave!: () => void;
  const pending = new Promise<void>((resolve) => { finishSave = resolve; });
  server.use(
    http.get("/api/world", () => HttpResponse.json(response)),
    http.put("/api/world/profile", async () => { await pending; return HttpResponse.json(response.profile); }),
  );
  renderWorld();
  await screen.findByLabelText(/watch symbols/i);
  fireEvent.click(screen.getByRole("button", { name: /save lens/i }));
  await waitFor(() => expect(screen.getByLabelText(/watch symbols/i)).toBeDisabled());
  expect(screen.getByLabelText(/^interests/i)).toBeDisabled();
  expect(screen.getByLabelText(/^regions/i)).toBeDisabled();
  finishSave();
  await waitFor(() => expect(screen.getByLabelText(/watch symbols/i)).toBeEnabled());
});

test("can repair corrupt saved preferences without editing the database", async () => {
  let repaired = false;
  server.use(
    http.get("/api/world", () => repaired ? HttpResponse.json(response) : HttpResponse.json({ detail: "Saved World preferences are invalid; save a new lens." }, { status: 503 })),
    http.put("/api/world/profile", async () => { repaired = true; return HttpResponse.json(response.profile); }),
  );
  renderWorld();
  expect(await screen.findByRole("alert")).toHaveTextContent(/saved world preferences are invalid/i);
  fireEvent.change(screen.getByLabelText(/watch symbols/i), { target: { value: "BP" } });
  fireEvent.click(screen.getByRole("button", { name: /save lens/i }));
  expect(await screen.findByText("Oil supply talks resume")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("rejects an overlong profile value before saving", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  fireEvent.change(await screen.findByLabelText(/^interests/i), { target: { value: "x".repeat(101) } });
  fireEvent.click(screen.getByRole("button", { name: /save lens/i }));
  expect(screen.getByRole("alert")).toHaveTextContent(/100 characters or fewer/i);
});

test("partial refresh reports failures and preserves cached event cards", async () => {
  let reads = 0;
  server.use(
    http.get("/api/world", () => HttpResponse.json(reads++ ? { ...response, items: [...response.items, { ...response.items[0], id: "new", title: "New cached event" }] } : response)),
    http.post("/api/world/refresh", () => HttpResponse.json({ updated: 7, failed: 1, skipped: 2 })),
  );
  renderWorld();
  await screen.findByText("Oil supply talks resume");
  fireEvent.click(screen.getByRole("button", { name: /^refresh sources$/i }));
  expect(await screen.findByText(/7 updated.*1 failed.*2 skipped/i)).toBeInTheDocument();
  expect(await screen.findByText("New cached event")).toBeInTheDocument();
});

test("a bad pinned reference surfaces the API detail and can be cleared", async () => {
  window.history.replaceState(null, "", "/world?book_ref=bad-ref");
  server.use(http.get("/api/world", ({ request }) => {
    const ref = new URL(request.url).searchParams.get("book_ref");
    return ref ? HttpResponse.json({ detail: "Pinned book reference is invalid" }, { status: 422 }) : HttpResponse.json(response);
  }));
  renderWorld();
  expect(await screen.findByText(/pinned book reference is invalid/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/pinned book reference/i)).toHaveValue("bad-ref");
  fireEvent.change(screen.getByLabelText(/pinned book reference/i), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Oil supply talks resume")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(new URL(window.location.href).searchParams.has("book_ref")).toBe(false);
});

test("labels observed timestamps and source access states without unsafe links", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  const observed = await screen.findByText("ECB publishes rate decision");
  expect(within(observed.closest("article")!).getByText(/observed/i)).toBeInTheDocument();
  expect(screen.getByText(/API setup required/i)).toBeInTheDocument();
  const xRow = screen.getByTestId("source-x");
  expect(within(xRow).queryByRole("link")).not.toBeInTheDocument();
});

test("reserves book styling for holding reasons and uses market styling for lens matches", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json({ ...response, items: [response.items[0], { ...response.items[1], id: "holding", reasons: ["Holding ASML mentioned"], matched_symbols: ["ASML"] }] })));
  renderWorld();
  const lensReason = (await screen.findByText("Matches interest: energy")).closest("aside");
  const holdingReason = screen.getByText("Holding ASML mentioned").closest("aside");
  expect(lensReason).toHaveClass("is-lens");
  expect(lensReason).not.toHaveClass("is-book");
  expect(holdingReason).toHaveClass("is-book");
});

test("shows stale warning without masking a source error", async () => {
  const broken = { ...response.sources[0], state: "error", stale: true, error: "Feed timed out" };
  server.use(http.get("/api/world", () => HttpResponse.json({ ...response, sources: [broken] })));
  renderWorld();
  const row = await screen.findByTestId("source-reuters");
  expect(within(row).getByText(/× error/i)).toHaveClass("state-error");
  expect(within(row).getByText(/▲ stale/i)).toHaveClass("state-stale");
  expect(within(row).getByText("Feed timed out")).toBeInTheDocument();
});

test("renders invalid dates safely and blocks unsafe event links", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json({ ...response, items: [{ ...response.items[0], published_at: "not-a-date", url: "javascript:alert(1)" }] })));
  renderWorld();
  expect(await screen.findByText(/invalid time/i)).toBeInTheDocument();
  expect(screen.getByText("Oil supply talks resume").closest("a")).toBeNull();
});

test("empty cache directs the user to refresh instead of clearing filters", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json({ ...response, items: [] })));
  renderWorld();
  expect(await screen.findByText(/refresh sources to fetch/i)).toBeInTheDocument();
  expect(screen.queryByText(/clear search/i)).not.toBeInTheDocument();
});

test("rejects a non-hex pinned reference locally", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  fireEvent.change(await screen.findByLabelText(/pinned book reference/i), { target: { value: "bad-ref" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/12 hexadecimal/i);
});

test("polls cached GET while another refresh is active and disables refresh", async () => {
  let reads = 0;
  server.use(http.get("/api/world", () => HttpResponse.json({ ...response, refreshing: reads++ === 0 })));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  renderWorld();
  expect(await screen.findByRole("button", { name: /refresh in progress/i })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  await waitFor(() => expect(screen.getByRole("button", { name: /refresh sources/i })).toBeEnabled());
  expect(reads).toBeGreaterThan(1);
  vi.useRealTimers();
});

test("polls the local cache every 30 seconds when idle without starting ingestion", async () => {
  let reads = 0;
  let refreshes = 0;
  server.use(
    http.get("/api/world", () => HttpResponse.json(reads++ ? { ...response, items: [...response.items, { ...response.items[0], id: "cli-event", title: "CLI cached event" }] } : response)),
    http.post("/api/world/refresh", () => { refreshes += 1; return HttpResponse.json({ updated: 0, failed: 0, skipped: 0 }); }),
  );
  vi.useFakeTimers({ shouldAdvanceTime: true });
  renderWorld();
  expect(await screen.findByText("Oil supply talks resume")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_100); });
  expect(await screen.findByText("CLI cached event")).toBeInTheDocument();
  expect(reads).toBeGreaterThan(1);
  expect(refreshes).toBe(0);
  vi.useRealTimers();
});

test("source health is exposed as a keyboard-focusable region", async () => {
  server.use(http.get("/api/world", () => HttpResponse.json(response)));
  renderWorld();
  const region = await screen.findByRole("region", { name: /source feed health/i });
  expect(region).toHaveAttribute("tabindex", "0");
});
