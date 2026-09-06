import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { World } from "../pages/World";
import { safeWorldUrl } from "../lib/world-url";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

test.each([
  "https://example.org/article?symbol=BP.L&source=rss#evidence",
  "https://www.ecb.europa.eu:443/press/政策.html",
  "https://xn--brse-5qa.de/news",
  "https://Example.ORG./article",
])("preserves valid DNS article evidence: %s", (url) => {
  expect(safeWorldUrl(url)).toBe(url);
});

test("unsafe cached article and source URLs remain readable without clickable links", async () => {
  const urls = [
    "http://127.1:8000/api/health", "http://0177.0.0.1/a", "http://0x7f.0.0.1/a",
    "http://%31%32%37.0.0.1/a", "http://2130706433/a", "http://[::1]/a",
    "http://[::ffff:127.0.0.1]/a", "http://192.168.1.1/a", "http://8.8.8.8/a",
    "http://[2606:4700:4700::1111]/a", "http://news.localhost./a", "http://news.local/a",
    "https://@example.org/a", "https://user:password@example.org/a",
    "https://example.org\\@127.0.0.1/a", "https://bad_label.example.org/a",
    "http://１２７。０。０。１/a", "https://example.org/\narticle", "javascript:alert(1)",
  ];
  const event = { id: "safe", source_id: "fixture", source_name: "Fixture", title: "Safe headline", url: "https://example.org/article?symbol=BP.L", summary: "", published_at: "2026-09-06T09:00:00Z", time_kind: "published", topics: [], regions: [], relevance: 0, reasons: [], matched_symbols: [] };
  const source = { id: "fixture", name: "Safe source", homepage: "https://example.org", category: "News", access: "public", description: "Fixture", enabled: true, state: "ok", last_attempt: null, last_success: null, next_refresh: null, item_count: 1, error: null, stale: false };
  server.use(http.get("/api/world", () => HttpResponse.json({
    items: [event, ...urls.map((url, i) => ({ ...event, id: `unsafe-${i}`, title: `Unsafe headline ${i}`, url }))],
    sources: [source, ...urls.map((homepage, i) => ({ ...source, id: `unsafe-${i}`, name: `Unsafe source ${i}`, homepage }))],
    profile: { watch_symbols: [], interests: [], regions: [] },
    context: { book_ref: null, symbols: [], label: "Personal lens" },
    as_of: "2026-09-06T09:00:00Z", refreshing: false,
  })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><World /></QueryClientProvider>);
  expect(await screen.findByRole("link", { name: "Safe headline" })).toHaveAttribute("href", event.url);
  expect(screen.getByRole("link", { name: "Safe source" })).toHaveAttribute("href", source.homepage);
  for (const [i] of urls.entries()) {
    expect(screen.getByText(`Unsafe headline ${i}`, { exact: true })).toBeInTheDocument();
    expect(within(screen.getByTestId(`source-unsafe-${i}`)).getByText(`Unsafe source ${i}`, { exact: true })).toBeInTheDocument();
  }
  expect(screen.queryAllByRole("link", { name: /^Unsafe (headline|source)/ })).toHaveLength(0);
});
