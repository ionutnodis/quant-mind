/**
 * InstrumentHover + InstrumentSheet tests (Task A2): hover shows the
 * name/type/exchange/1d/vol/beta tooltip from the mocked /api/instruments
 * endpoint, click opens the InstrumentSheet floating window with the candle
 * chart (stubbed — Plotly needs real canvas/WebGL in jsdom, pattern:
 * macro.test.tsx) + stats + TradingView/issuer link-outs, and missing
 * metadata renders an honest fallback rather than crashing.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { InstrumentHover } from "../components/InstrumentHover";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

vi.mock("../components/CandleChart", () => ({
  CandleChart: () => <div data-testid="candle-chart" />,
}));

function renderHover(
  props: Partial<React.ComponentProps<typeof InstrumentHover>> = {},
  container?: HTMLElement,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InstrumentHover symbol="EEM" change1d={0.012} {...props}>
        EEM
      </InstrumentHover>
    </QueryClientProvider>,
    container ? { container } : undefined,
  );
}

const INSTRUMENT = {
  symbol: "EEM",
  con_id: 2,
  long_name: "iShares MSCI Emerging Markets ETF",
  exchange: "ARCA",
  currency: "USD",
  sec_type: "STK",
  industry: null,
  region: "Emerging Markets",
  provider: "ibkr",
  isin: null,
  primary_exchange: "ARCA",
  local_symbol: "EEM",
  trading_class: "EEM",
  stock_type: "ETF",
  valid_exchanges: ["SMART", "ARCA"],
  issuer_id: null,
  ucits_profile_status: null,
  ucits_profile_reason: null,
  ucits_profile: null,
  last_close: 42.5,
  high_52w: 45.0,
  low_52w: 38.0,
  pct_from_52w_high: -0.0556,
  pct_from_52w_low: 0.1184,
  ann_vol: 0.21,
  beta: 0.85,
  beta_benchmark: "SPY",
  risk_base_currency: "USD",
  risk_fx_source: null,
  risk_fx_as_of: null,
  risk: {
    status: "ready",
    reason: null,
    benchmark: "SPY",
    base_currency: "USD",
    fx: {
      status: "identity",
      base_currency: "USD",
      source: null,
      as_of: null,
      fetched_at: null,
      missing_currencies: [],
      note: "All analytical prices are denominated in USD.",
    },
    note: "Volatility and beta are ready from USD-normalized history.",
  },
  as_of: "2026-07-24T00:00:00Z",
};

const CANDLES = {
  symbol: "EEM",
  days: 180,
  candles: [
    { date: "2026-07-23T00:00:00Z", open: 42.0, high: 43.0, low: 41.5, close: 42.5, volume: 1000.0 },
    { date: "2026-07-24T00:00:00Z", open: 42.5, high: 43.5, low: 42.0, close: 43.0, volume: 1100.0 },
  ],
};

test("hovering the trigger shows name/type/exchange/1d/vol/beta tooltip", async () => {
  server.use(http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)));
  renderHover();

  const trigger = screen.getByTestId("instrument-trigger-EEM");
  fireEvent.mouseEnter(trigger.parentElement!);

  expect(await screen.findByText("iShares MSCI Emerging Markets ETF")).toBeInTheDocument();
  expect(screen.getByText(/ARCA/)).toBeInTheDocument();
  expect(screen.getByText("1.20%")).toBeInTheDocument(); // 1D from change1d prop
  expect(screen.getByText("21.00%")).toBeInTheDocument(); // ann vol
  expect(screen.getByText("0.85")).toBeInTheDocument(); // beta

  fireEvent.mouseLeave(trigger.parentElement!);
  await waitFor(() => expect(screen.queryByTestId("instrument-hover-EEM")).not.toBeInTheDocument());
});

test("keyboard focus describes the tooltip and Escape or blur dismisses it", async () => {
  server.use(http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)));
  renderHover();
  const trigger = screen.getByTestId("instrument-trigger-EEM");

  fireEvent.focus(trigger);

  const tooltip = await screen.findByRole("tooltip");
  expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);

  fireEvent.keyDown(trigger, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  expect(trigger).not.toHaveAttribute("aria-describedby");

  fireEvent.blur(trigger);
  fireEvent.focus(trigger);
  expect(await screen.findByRole("tooltip")).toBeInTheDocument();
  fireEvent.blur(trigger);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});

test("symbol punctuation never enters ARIA reference IDs", async () => {
  const symbol = "BRK.B";
  server.use(
    http.get("/api/instruments/BRK.B", () =>
      HttpResponse.json({ ...INSTRUMENT, symbol })
    ),
    http.get("/api/instruments/BRK.B/candles", () =>
      HttpResponse.json({ ...CANDLES, symbol })
    ),
  );
  renderHover({ symbol, children: symbol });
  const trigger = screen.getByTestId(`instrument-trigger-${symbol}`);

  fireEvent.focus(trigger);
  const tooltip = await screen.findByRole("tooltip");
  expect(tooltip.id).toMatch(/^instrument-tooltip-[a-z0-9-]+$/i);
  expect(tooltip.id).not.toContain(".");
  expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);

  fireEvent.click(trigger);
  const sheet = await screen.findByRole("dialog");
  const titleId = sheet.getAttribute("aria-labelledby");
  expect(titleId).toMatch(/^instrument-sheet-title-[a-z0-9-]+$/i);
  expect(titleId).not.toContain(".");
  expect(document.getElementById(titleId!)).toHaveTextContent("BRK.B instrument detail");
});

test("hover loading is announced to assistive technology", async () => {
  server.use(
    http.get("/api/instruments/EEM", async () => {
      await delay("infinite");
      return HttpResponse.json(INSTRUMENT);
    }),
  );
  renderHover();

  fireEvent.focus(screen.getByTestId("instrument-trigger-EEM"));

  expect(
    await screen.findByRole("status", { name: "Loading EEM instrument" }),
  ).toBeInTheDocument();
});

test("tooltip renders honestly when instrument metadata is missing (nulls)", async () => {
  server.use(
    http.get("/api/instruments/EEM", () =>
      HttpResponse.json({
        ...INSTRUMENT,
        long_name: null,
        exchange: null,
        currency: null,
        ann_vol: null,
        beta: null,
      })
    )
  );
  renderHover({ change1d: null });
  fireEvent.mouseEnter(screen.getByTestId("instrument-trigger-EEM").parentElement!);

  expect(await screen.findByText("No metadata cached yet")).toBeInTheDocument();
  // Multiple "—" placeholders render for the missing 1d/vol/beta fields — no crash.
  expect(screen.getAllByText("—").length).toBeGreaterThan(0);
});

test("clicking the trigger opens InstrumentSheet with chart, stats, and link-outs", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json(CANDLES))
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  const sheet = await screen.findByTestId("instrument-sheet-EEM");
  expect(sheet).toBeInTheDocument();
  expect(sheet).toHaveAttribute("aria-modal", "true");
  const close = screen.getByTestId("instrument-sheet-close");
  expect(close).toHaveFocus();
  await waitFor(() => expect(screen.getByTestId("candle-chart")).toBeInTheDocument());

  const sheetHeader = close.closest("header");
  expect(sheetHeader).not.toBeNull();
  expect(within(sheetHeader!).getByText("as of 2026-07-24")).toBeInTheDocument();

  expect(screen.getAllByText(/iShares MSCI Emerging Markets ETF/).length).toBeGreaterThan(0);
  expect(screen.getByRole("region", { name: /risk evidence/i })).toHaveTextContent(
    "Reporting USD · FX identity · as of not required",
  );
  const tvLink = screen.getByRole("link", { name: /TradingView/i });
  expect(tvLink).toHaveAttribute("href", "https://www.tradingview.com/chart/?symbol=EEM");
  expect(tvLink).toHaveAttribute("target", "_blank");
  const issuerLink = screen.getByRole("link", { name: /Issuer/i });
  expect(issuerLink.getAttribute("href")).toContain("google.com/finance/quote/EEM");

  // hovering while the sheet is open must not also show the hover tooltip
  expect(screen.queryByTestId("instrument-hover-EEM")).not.toBeInTheDocument();

  fireEvent.click(screen.getByTestId("instrument-sheet-close"));
  await waitFor(() => expect(screen.queryByTestId("instrument-sheet-EEM")).not.toBeInTheDocument());
  expect(screen.getByTestId("instrument-trigger-EEM")).toHaveFocus();
});

test("sheet keeps metadata visible while chart data loads and announces the wait", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", async () => {
      await delay("infinite");
      return HttpResponse.json(CANDLES);
    }),
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  expect(
    await screen.findByText("iShares MSCI Emerging Markets ETF"),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("status", { name: "Loading EEM chart" }),
  ).toBeInTheDocument();
});

test("sheet explains why risk metrics are unavailable", async () => {
  const fxUnavailable = {
    ...INSTRUMENT,
    ann_vol: null,
    beta: null,
    risk: {
      status: "unavailable",
      reason: "fx_unavailable",
      benchmark: "SPY",
      base_currency: "USD",
      fx: {
        status: "incomplete",
        base_currency: "USD",
        source: null,
        as_of: null,
        fetched_at: null,
        missing_currencies: ["EUR"],
        note: "Dated FX normalization to USD is unavailable for EUR.",
      },
      note: "Risk metrics are unavailable because dated FX evidence is missing.",
    },
  };
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(fxUnavailable)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json(CANDLES)),
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  expect(
    await screen.findByText(
      "Risk metrics are unavailable because dated FX evidence is missing.",
    ),
  ).toBeInTheDocument();
});

test("Escape closes the instrument dialog and restores trigger focus", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json(CANDLES)),
  );
  renderHover();
  const trigger = screen.getByTestId("instrument-trigger-EEM");
  fireEvent.click(trigger);
  await screen.findByRole("dialog", { name: /EEM instrument detail/i });

  fireEvent.keyDown(document, { key: "Escape" });

  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(trigger).toHaveFocus();
});

test("sheet traps focus and restores the inert application root on close", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json(CANDLES)),
  );
  const appRoot = document.createElement("div");
  appRoot.id = "root";
  document.body.append(appRoot);
  const view = renderHover({}, appRoot);
  const trigger = screen.getByTestId("instrument-trigger-EEM");
  fireEvent.click(trigger);

  const close = await screen.findByTestId("instrument-sheet-close");
  const lastLink = await screen.findByRole("link", { name: /Issuer/i });
  expect(appRoot).toHaveAttribute("inert");
  expect(document.body.style.overflow).toBe("hidden");
  expect(close).toHaveFocus();

  fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
  expect(lastLink).toHaveFocus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(close).toHaveFocus();

  fireEvent.click(close);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(appRoot).not.toHaveAttribute("inert");
  expect(document.body.style.overflow).toBe("");
  expect(trigger).toHaveFocus();
  view.unmount();
  appRoot.remove();
});

test("sheet shows an honest empty state when no candles are cached", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => HttpResponse.json({ ...CANDLES, candles: [] }))
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  expect(await screen.findByText(/no cached candles yet/i)).toBeInTheDocument();
});

test("sheet distinguishes a candle request failure from an empty cache", async () => {
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () =>
      HttpResponse.json({ detail: "cache corrupt" }, { status: 500 })
    ),
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  expect(await screen.findByRole("alert")).toHaveTextContent(/candle data failed/i);
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  expect(screen.queryByText(/no cached candles yet/i)).not.toBeInTheDocument();
});

test("sheet retries a failed candle request and renders the recovered chart", async () => {
  let candleRequests = 0;
  server.use(
    http.get("/api/instruments/EEM", () => HttpResponse.json(INSTRUMENT)),
    http.get("/api/instruments/EEM/candles", () => {
      candleRequests += 1;
      return candleRequests === 1
        ? HttpResponse.json({ detail: "cache temporarily unavailable" }, { status: 500 })
        : HttpResponse.json(CANDLES);
    }),
  );
  renderHover();
  fireEvent.click(screen.getByTestId("instrument-trigger-EEM"));

  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));

  expect(await screen.findByTestId("candle-chart")).toBeInTheDocument();
  expect(candleRequests).toBe(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("sheet describes fresh European ETF facts as sourced, not regulatory verification", async () => {
  const ucits = {
    ...INSTRUMENT,
    symbol: "SWDA",
    isin: "IE00B4L5Y983",
    primary_exchange: "LSEETF",
    currency: "GBP",
    ucits_profile_status: "FRESH",
    ucits_profile: {
      schema_version: "ucits_etf_profile_v1",
      isin: "IE00B4L5Y983",
      fund_name: "iShares Core MSCI World UCITS ETF USD (Acc)",
      issuer: "iShares",
      domicile: "Ireland",
      ter_pct: "0.20",
      distribution_policy: "ACCUMULATING",
      replication_method: "Physical · Optimized sampling",
      benchmark_name: "MSCI World",
      provenance: {
        source: "justetf",
        source_url: "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
        fetched_at_utc: "2026-09-04T09:30:00Z",
      },
    },
  };
  server.use(
    http.get("/api/instruments/SWDA", () => HttpResponse.json(ucits)),
    http.get("/api/instruments/SWDA/candles", () =>
      HttpResponse.json({ ...CANDLES, symbol: "SWDA" })
    )
  );

  renderHover({ symbol: "SWDA", children: "SWDA" });
  fireEvent.click(screen.getByTestId("instrument-trigger-SWDA"));

  expect(await screen.findByRole("region", { name: "European ETF sourced profile" })).toBeInTheDocument();
  expect(screen.getByText("● Source cache fresh")).toBeInTheDocument();
  expect(screen.queryByText(/verified cache/i)).not.toBeInTheDocument();
  expect(screen.getByText("0.20%")).toBeInTheDocument();
  expect(screen.getByText("MSCI World")).toBeInTheDocument();
  expect(screen.getAllByText(/IE00B4L5Y983/).length).toBeGreaterThan(0);
  const source = screen.getByRole("link", { name: /justETF source/i });
  expect(source).toHaveAttribute(
    "href",
    "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983"
  );
  expect(screen.getByText("ibkr")).toBeInTheDocument();
});

test("sheet explains a stale UCITS profile without rendering stale facts", async () => {
  server.use(
    http.get("/api/instruments/SWDA", () =>
      HttpResponse.json({
        ...INSTRUMENT,
        symbol: "SWDA",
        isin: "IE00B4L5Y983",
        ucits_profile_status: "STALE",
        ucits_profile_reason: "cached UCITS profile exceeds the 30-day freshness window; run sync",
        ucits_profile: null,
      })
    ),
    http.get("/api/instruments/SWDA/candles", () =>
      HttpResponse.json({ ...CANDLES, symbol: "SWDA" })
    )
  );

  renderHover({ symbol: "SWDA", children: "SWDA" });
  fireEvent.click(screen.getByTestId("instrument-trigger-SWDA"));

  expect(await screen.findByText("▲ Stale European ETF sourced profile")).toBeInTheDocument();
  expect(screen.getByText(/30-day freshness window/i)).toBeInTheDocument();
  expect(screen.queryByText("● Source cache fresh")).not.toBeInTheDocument();
});

test("sheet withholds a non-finite TER instead of rendering NaN", async () => {
  server.use(
    http.get("/api/instruments/SWDA", () =>
      HttpResponse.json({
        ...INSTRUMENT,
        symbol: "SWDA",
        isin: "IE00B4L5Y983",
        ucits_profile_status: "FRESH",
        ucits_profile: {
          schema_version: "ucits_etf_profile_v1",
          isin: "IE00B4L5Y983",
          fund_name: "Example European ETF",
          issuer: "Example issuer",
          domicile: "Ireland",
          ter_pct: "NaN",
          distribution_policy: "ACCUMULATING",
          replication_method: "Physical",
          benchmark_name: "Example index",
          provenance: {
            source: "justetf",
            source_url: "https://www.justetf.com/en/etf-profile.html?isin=IE00B4L5Y983",
            fetched_at_utc: "2026-09-04T09:30:00Z",
          },
        },
      })
    ),
    http.get("/api/instruments/SWDA/candles", () =>
      HttpResponse.json({ ...CANDLES, symbol: "SWDA" })
    ),
  );

  renderHover({ symbol: "SWDA", children: "SWDA" });
  fireEvent.click(screen.getByTestId("instrument-trigger-SWDA"));

  const profile = await screen.findByRole("region", { name: "European ETF sourced profile" });
  const terLabel = within(profile).getByText("TER");
  expect(terLabel.parentElement).toHaveTextContent("TER—");
  expect(profile).not.toHaveTextContent("NaN%");
});
