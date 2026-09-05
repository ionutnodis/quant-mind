import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { Setup } from "../pages/Setup";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const EMPTY_STATUS = {
  overall: "needs_attention",
  api: { status: "ready", version: "0.5.0.0" },
  broker: { status: "unavailable", provider: "IBKR", mode: "paper", error: null },
  market_data: {
    status: "empty",
    symbols: 0,
    ready_symbols: 0,
    missing_symbols: [],
    stale_symbols: [],
    corrupt_symbols: [],
    series: 0,
    as_of: null,
    age_days: null,
    portfolio_discovery_error: null,
  },
  macro_data: {
    status: "empty",
    required_series: 4,
    ready_series: 0,
    missing_series: ["NET_LIQUIDITY", "US10Y", "US2Y", "US3M"],
    stale_series: [],
    corrupt_series: [],
    as_of: null,
    age_days: null,
  },
  options_data: {
    status: "not_required",
    total_positions: 0,
    priced_positions: 0,
    missing_contracts: [],
    stale_chains: [],
    chain_as_of: null,
    chain_age_days: null,
  },
  fx_data: {
    status: "not_required",
    base_currency: "USD",
    required_currencies: [],
    missing_currencies: [],
    provider: null,
    as_of: null,
  },
  ucits_data: {
    status: "not_required",
    total_etfs: 0,
    ready_profiles: 0,
    missing_symbols: [],
    stale_symbols: [],
  },
  book: {
    status: "not_pinned",
    snapshot_count: 0,
    latest_snapshot_id: null,
    valuation_ts: null,
    option_positions: 0,
    age_days: null,
    source: null,
    account_fingerprint: null,
    broker_mode: null,
    unsupported_currencies: [],
    unsupported_security_types: [],
    reason: null,
  },
  next_action: "start_gateway",
};

function renderSetup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Setup />
    </QueryClientProvider>
  );
  return { ...view, client };
}

test("shows the exact first action and the state of every setup dependency", async () => {
  server.use(http.get("/api/setup/status", () => HttpResponse.json(EMPTY_STATUS)));

  renderSetup();

  expect(await screen.findByRole("heading", { name: "Finish local setup" })).toBeInTheDocument();
  expect(screen.getByTestId("overall-status")).toHaveTextContent("▲ ACTION REQUIRED · v0.5.0.0");
  expect(screen.getByText("Start IBKR Gateway or TWS")).toBeInTheDocument();
  const broker = within(screen.getByLabelText("IBKR Gateway status"));
  const market = within(screen.getByLabelText("Market cache status"));
  const macro = within(screen.getByLabelText("Macro evidence status"));
  const options = within(screen.getByLabelText("Held option evidence status"));
  const fx = within(screen.getByLabelText("FX evidence status"));
  const ucits = within(screen.getByLabelText("European ETF sourced-profile status"));
  expect(ucits.getByText("European ETF profiles")).toBeInTheDocument();
  const book = within(screen.getByLabelText("Current book status"));
  expect(broker.getByText("Unavailable")).toBeInTheDocument();
  expect(broker.getByTestId("status-glyph")).toHaveTextContent("×");
  expect(market.getByText("Empty")).toBeInTheDocument();
  expect(market.getByTestId("status-glyph")).toHaveTextContent("◇");
  expect(macro.getByText("Empty")).toBeInTheDocument();
  expect(options.getByText("Not required")).toBeInTheDocument();
  expect(fx.getByText("Not required")).toBeInTheDocument();
  expect(ucits.getByText("Not required")).toBeInTheDocument();
  expect(book.getByText("Not pinned")).toBeInTheDocument();
  expect(book.getByTestId("status-glyph")).toHaveTextContent("◇");
  expect(screen.getByTestId("setup-readiness-grid")).toHaveClass(
    "grid-cols-1",
    "sm:grid-cols-2",
    "xl:grid-cols-4"
  );
});

test("explains the recoverable option-currency action without offering a dead-end rebase", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        overall: "needs_attention",
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        book: {
          ...EMPTY_STATUS.book,
          status: "unsupported",
          snapshot_count: 1,
          latest_snapshot_id: "abc123def456",
          valuation_ts: "2026-09-04T13:00:00Z",
          option_positions: 1,
          age_days: 0,
          source: "manual",
          unsupported_currencies: ["EUR"],
          reason: "cross_currency_option",
        },
        next_action: "resolve_option_currency",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText("Align the option book currency")).toBeInTheDocument();
  expect(screen.getByText(/Set QM_BASE_CURRENCY/)).toBeInTheDocument();
  expect(screen.getByText(/multiple currencies is unsupported/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /rebase/i })).not.toBeInTheDocument();
});

test("explains a failed live portfolio discovery without leaking its cache sentinel", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        market_data: {
          ...EMPTY_STATUS.market_data,
          status: "incomplete",
          symbols: 1,
          ready_symbols: 1,
          as_of: "2026-09-04",
          portfolio_discovery_error: "live_portfolio_unavailable",
        },
        next_action: "sync_market_data",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText("Retry live portfolio discovery")).toBeInTheDocument();
  expect(screen.getByText(/could not read the IBKR portfolio/)).toBeInTheDocument();
  expect(screen.getByText(/live IBKR portfolio unavailable/)).toBeInTheDocument();
  expect(screen.queryByText(/__LIVE_PORTFOLIO_DISCOVERY_FAILED__/)).not.toBeInTheDocument();
});

test("names a base-currency change when the book must be pinned again", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        fx_data: { ...EMPTY_STATUS.fx_data, base_currency: "GBP" },
        book: {
          ...EMPTY_STATUS.book,
          status: "stale",
          snapshot_count: 1,
          latest_snapshot_id: "abc123def456",
          valuation_ts: "2026-09-04T13:00:00Z",
          age_days: 0,
          source: "manual",
          reason: "base_currency_mismatch",
        },
        next_action: "pin_book",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText("Re-pin the book in the analysis currency")).toBeInTheDocument();
  expect(screen.getByText(/immutable GBP reference/)).toBeInTheDocument();
  expect(screen.queryByText(/belongs to a different broker scope/)).not.toBeInTheDocument();
});

test("names an instrument identity change when the book must be pinned again", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        book: {
          ...EMPTY_STATUS.book,
          status: "stale",
          snapshot_count: 1,
          latest_snapshot_id: "abc123def456",
          valuation_ts: "2026-09-04T13:00:00Z",
          age_days: 0,
          source: "manual",
          reason: "instrument_identity_mismatch",
        },
        next_action: "pin_book",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText("Re-pin the book after the instrument update")).toBeInTheDocument();
  expect(screen.getByText(/symbol now resolves to a different contract/)).toBeInTheDocument();
});

test("syncs missing dated FX evidence using the normal sync job", async () => {
  const missingFx = {
    ...EMPTY_STATUS,
    broker: { ...EMPTY_STATUS.broker, status: "connected" },
    fx_data: {
      status: "missing",
      base_currency: "GBP",
      required_currencies: ["EUR"],
      missing_currencies: ["EUR"],
      provider: null,
      as_of: null,
    },
    next_action: "sync_fx_data",
  };
  server.use(
    http.get("/api/setup/status", () => HttpResponse.json(missingFx)),
    http.post("/api/sync", () => HttpResponse.json({ job_id: "fx-sync" })),
    http.get("/api/sync/fx-sync", () =>
      HttpResponse.json({ state: "done", result: "synced ECB FX", error: null })
    )
  );

  renderSetup();
  expect(await screen.findByText("Sync dated FX evidence")).toBeInTheDocument();
  const fx = within(screen.getByLabelText("FX evidence status"));
  expect(fx.getByText("Missing")).toBeInTheDocument();
  expect(fx.getByText(/EUR → GBP/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Sync FX data" }));
  expect(await screen.findByText("synced ECB FX")).toBeInTheDocument();
});

test("syncs stale evidence and refreshes readiness without reloading the page", async () => {
  let statusRequests = 0;
  let syncSubmitted = false;
  const stale = {
    ...EMPTY_STATUS,
    broker: { ...EMPTY_STATUS.broker, status: "connected" },
    market_data: {
      status: "stale",
      symbols: 11,
      series: 4,
      as_of: "2026-08-20",
      age_days: 15,
    },
    next_action: "sync_market_data",
  };
  const fresh = {
    ...stale,
    market_data: { ...stale.market_data, status: "ready", as_of: "2026-09-04", age_days: 0 },
    next_action: "pin_book",
  };
  server.use(
    http.get("/api/setup/status", () => {
      statusRequests += 1;
      return HttpResponse.json(syncSubmitted ? fresh : stale);
    }),
    http.post("/api/sync", () => {
      syncSubmitted = true;
      return HttpResponse.json({ job_id: "sync-1" });
    }),
    http.get("/api/sync/sync-1", () =>
      HttpResponse.json({ state: "done", result: "synced 11 symbols", error: null })
    )
  );

  renderSetup();
  const button = await screen.findByRole("button", { name: "Sync market data" });
  fireEvent.click(button);

  await waitFor(() =>
    expect(within(screen.getByLabelText("Market cache status")).getByText("Ready")).toBeInTheDocument()
  );
  expect(statusRequests).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("synced 11 symbols")).toBeInTheDocument();
});

test("pins the live IBKR book and exposes the same snapshot to analysis pages", async () => {
  let pinned = false;
  const beforePin = {
    ...EMPTY_STATUS,
    broker: { ...EMPTY_STATUS.broker, status: "connected" },
    market_data: {
      status: "ready",
      symbols: 11,
      series: 4,
      as_of: "2026-09-04",
      age_days: 0,
    },
    next_action: "pin_book",
  };
  const afterPin = {
    ...beforePin,
    overall: "ready",
    book: {
      status: "ready",
      snapshot_count: 1,
      latest_snapshot_id: "abc123def456",
      valuation_ts: "2026-09-04T13:00:00Z",
      option_positions: 0,
    },
    next_action: "ready",
  };
  server.use(
    http.get("/api/setup/status", () => HttpResponse.json(pinned ? afterPin : beforePin)),
    http.post("/api/book/pin", () => {
      pinned = true;
      return HttpResponse.json({
        snapshot_id: "abc123def456",
        valuation_ts: "2026-09-04T13:00:00Z",
        base_currency: "USD",
        positions: [{ symbol: "NVDA", qty: 100, con_id: 4815747, sec_type: "STK", multiplier: 1 }],
      });
    })
  );

  renderSetup();
  const pinButton = await screen.findByRole("button", { name: "Pin current book" });
  expect(screen.getByText(/Portfolio, What-If, and Hedge Lab analyse the same positions/)).toBeInTheDocument();
  expect(screen.queryByText(/Risk, What-If, and Hedge Lab analyse the same positions/)).not.toBeInTheDocument();
  expect(pinButton.closest(".authoring-only")).not.toBeNull();
  expect(pinButton).not.toHaveClass("border-you", "text-you");
  fireEvent.click(pinButton);

  expect(await screen.findByText("Book pinned · abc123def456")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("overall-status")).toHaveTextContent("● READY · v0.5.0.0"));
  expect(screen.getByRole("link", { name: "Open Portfolio" })).toHaveAttribute(
    "href",
    "/portfolio?book_ref=abc123def456"
  );
  expect(screen.getByRole("link", { name: "Open What-If" })).toHaveAttribute(
    "href",
    "/whatif?book_ref=abc123def456"
  );
  expect(screen.getByRole("link", { name: "Open Hedge Lab" })).toHaveAttribute(
    "href",
    "/hedge?book_ref=abc123def456"
  );
  expect(screen.getByRole("button", { name: "Refresh current book" })).toBeInTheDocument();
});

test("withholds analysis links until a stale snapshot is refreshed", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        market_data: {
          ...EMPTY_STATUS.market_data,
          status: "ready",
          symbols: 1,
          ready_symbols: 1,
          as_of: "2026-09-04",
          age_days: 0,
        },
        book: {
          ...EMPTY_STATUS.book,
          status: "stale",
          snapshot_count: 1,
          latest_snapshot_id: "stale123abcd",
          valuation_ts: "2026-09-03T13:00:00Z",
          age_days: 1,
          source: "live_ibkr",
          account_fingerprint: "a1b2c3d4e5f6",
          broker_mode: "paper",
          reason: "stale_snapshot",
        },
        next_action: "pin_book",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText("Refresh the pinned book")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open Portfolio" })).not.toBeInTheDocument();
});

test("removes pinned-book analysis links when refreshed readiness becomes non-ready", async () => {
  const beforePin = {
    ...EMPTY_STATUS,
    broker: { ...EMPTY_STATUS.broker, status: "connected" },
    market_data: {
      ...EMPTY_STATUS.market_data,
      status: "ready",
      symbols: 1,
      ready_symbols: 1,
      series: 4,
      as_of: "2026-09-04",
      age_days: 0,
    },
    macro_data: {
      ...EMPTY_STATUS.macro_data,
      status: "ready",
      ready_series: 4,
      missing_series: [],
      as_of: "2026-09-04",
      age_days: 0,
    },
    next_action: "pin_book",
  };
  const ready = {
    ...beforePin,
    overall: "ready",
    book: {
      ...EMPTY_STATUS.book,
      status: "ready",
      snapshot_count: 1,
      latest_snapshot_id: "fresh-book-ref",
      valuation_ts: "2026-09-04T13:00:00Z",
      age_days: 0,
    },
    next_action: "ready",
  };
  const degraded = {
    ...ready,
    overall: "needs_attention",
    market_data: {
      ...ready.market_data,
      status: "stale",
      as_of: "2026-08-20",
      age_days: 15,
    },
    next_action: "sync_market_data",
  };
  let currentStatus: Record<string, unknown> = beforePin;
  server.use(
    http.get("/api/setup/status", () => HttpResponse.json(currentStatus)),
    http.post("/api/book/pin", () => {
      currentStatus = ready;
      return HttpResponse.json({
        snapshot_id: "fresh-book-ref",
        valuation_ts: "2026-09-04T13:00:00Z",
        base_currency: "USD",
        positions: [{ symbol: "NVDA", qty: 10, con_id: 1, sec_type: "STK", multiplier: 1 }],
      });
    }),
  );

  const { client } = renderSetup();
  fireEvent.click(await screen.findByRole("button", { name: "Pin current book" }));
  expect(await screen.findByRole("link", { name: "Open Portfolio" })).toBeInTheDocument();

  currentStatus = degraded;
  await client.invalidateQueries({ queryKey: ["setup-status"] });

  await screen.findByText("Sync the market cache");
  expect(screen.queryByRole("link", { name: "Open Portfolio" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open What-If" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open Hedge Lab" })).not.toBeInTheDocument();
});

test("does not route an option book into equity-only What-If and Hedge calculations", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        overall: "ready",
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        market_data: {
          status: "ready",
          symbols: 11,
          series: 4,
          as_of: "2026-09-04",
          age_days: 0,
        },
        book: {
          status: "ready",
          snapshot_count: 1,
          latest_snapshot_id: "optionbook12",
          valuation_ts: "2026-09-04T13:00:00Z",
          option_positions: 2,
        },
        options_data: {
          status: "ready",
          total_positions: 2,
          priced_positions: 2,
          missing_contracts: [],
          stale_chains: [],
          chain_as_of: "2026-09-04",
          chain_age_days: 0,
        },
        next_action: "ready",
      })
    )
  );

  renderSetup();

  expect(await screen.findByText(/2 option positions are preserved/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open Portfolio" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open What-If" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open Hedge Lab" })).not.toBeInTheDocument();
});

test("surfaces a pin failure and keeps the action available for retry", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        market_data: {
          status: "ready",
          symbols: 11,
          series: 4,
          as_of: "2026-09-04",
          age_days: 0,
        },
        next_action: "pin_book",
      })
    ),
    http.post("/api/book/pin", () =>
      HttpResponse.json({ detail: "broker portfolio unavailable" }, { status: 503 })
    )
  );

  renderSetup();
  fireEvent.click(await screen.findByRole("button", { name: "Pin current book" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(/broker portfolio unavailable/i);
  expect(screen.getByRole("button", { name: "Pin current book" })).toBeEnabled();
});

test("shows a cancelled sync and allows the user to retry", async () => {
  server.use(
    http.get("/api/setup/status", () =>
      HttpResponse.json({
        ...EMPTY_STATUS,
        broker: { ...EMPTY_STATUS.broker, status: "connected" },
        next_action: "sync_market_data",
      })
    ),
    http.post("/api/sync", () => HttpResponse.json({ job_id: "sync-cancelled" })),
    http.get("/api/sync/sync-cancelled", () =>
      HttpResponse.json({ state: "cancelled", result: null, error: null })
    )
  );

  renderSetup();
  fireEvent.click(await screen.findByRole("button", { name: "Sync market data" }));

  expect(await screen.findByText("Sync cancelled")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sync market data" })).toBeEnabled();
});
