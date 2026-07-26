/**
 * NewsTicker tests (wave-3B Today task): renders headlines from the mocked
 * GET /api/news, click-through when a url is present, honest empty state
 * when the feed is unavailable, and pauses the scroll animation on hover.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { NewsTicker } from "../components/NewsTicker";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

vi.mock("../components/InstrumentHover", () => ({
  InstrumentHover: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

function renderTicker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewsTicker />
    </QueryClientProvider>
  );
}

test("renders headlines with source/symbol from the API", async () => {
  server.use(
    http.get("/api/news", () =>
      HttpResponse.json({
        items: [
          { time: "2026-07-26T12:00:00Z", source: "BRFG", headline: "Fed holds rates steady", symbol: null },
          { time: "2026-07-26T11:00:00Z", source: "DJNL", headline: "SPY rallies into the close", symbol: "SPY" },
        ],
        as_of: "2026-07-26T12:00:00Z",
        note: null,
      })
    )
  );
  renderTicker();
  expect((await screen.findAllByText("Fed holds rates steady")).length).toBeGreaterThan(0);
  expect(screen.getAllByText("SPY rallies into the close").length).toBeGreaterThan(0);
  expect(screen.getAllByText("SPY").length).toBeGreaterThan(0);
  // items render twice (seamless loop track)
  expect(screen.getAllByTestId("news-item").length).toBe(4);
});

test("click-through renders as a link when url is present, plain row otherwise", async () => {
  server.use(
    http.get("/api/news", () =>
      HttpResponse.json({
        items: [
          {
            time: "2026-07-26T12:00:00Z",
            source: "BRFG",
            headline: "Linked headline",
            symbol: null,
            url: "https://example.com/a",
          },
          { time: "2026-07-26T11:00:00Z", source: "DJNL", headline: "Unlinked headline", symbol: null },
        ],
        as_of: "2026-07-26T12:00:00Z",
        note: null,
      })
    )
  );
  renderTicker();
  const linked = (await screen.findAllByText("Linked headline"))[0].closest("a");
  expect(linked).toHaveAttribute("href", "https://example.com/a");
  expect(linked).toHaveAttribute("target", "_blank");

  const unlinked = screen.getAllByText("Unlinked headline")[0].closest("div[data-testid='news-item']");
  expect(unlinked).toBeInTheDocument();
  expect(unlinked?.closest("a")).toBeNull();
});

test("honest empty state when the feed is unavailable", async () => {
  server.use(
    http.get("/api/news", () =>
      HttpResponse.json({
        items: [],
        as_of: null,
        note: "news source unavailable — Gateway down or no entitled providers",
      })
    )
  );
  renderTicker();
  expect(await screen.findByTestId("news-empty")).toHaveTextContent(/unavailable/);
});

test("pauses the scroll animation on hover and resumes on mouse-leave", async () => {
  server.use(
    http.get("/api/news", () =>
      HttpResponse.json({
        items: [{ time: "2026-07-26T12:00:00Z", source: "BRFG", headline: "Fed holds rates steady", symbol: null }],
        as_of: "2026-07-26T12:00:00Z",
        note: null,
      })
    )
  );
  renderTicker();
  const ticker = await screen.findByTestId("news-ticker");
  const track = screen.getByTestId("news-ticker-track");
  expect(track.style.animationPlayState).toBe("running");

  fireEvent.mouseEnter(ticker);
  await waitFor(() => expect(track.style.animationPlayState).toBe("paused"));

  fireEvent.mouseLeave(ticker);
  await waitFor(() => expect(track.style.animationPlayState).toBe("running"));
});
