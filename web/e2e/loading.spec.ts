import { expect, test } from "@playwright/test";

test("empty Today downloads less than 500 kB of JavaScript without charts", async ({ page }) => {
  await page.route("**/api/brief", (route) => route.fulfill({ json: { tiles: [], as_of: null, benchmark_es: null } }));
  const sizes: Promise<number>[] = [];
  page.on("response", (response) => {
    if (response.request().resourceType() === "script") sizes.push(response.body().then((body) => body.length));
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Cache empty" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect((await Promise.all(sizes)).reduce((sum, bytes) => sum + bytes, 0)).toBeLessThan(500_000);
});

test("a failed World page download leaves navigation working and can recover on reload", async ({ page }) => {
  const worldChunk = /\/assets\/World-[^/]+\.js$/;
  const pin = await page.request.post("/api/book/pin", { data: { positions: [{ symbol: "SPY", qty: 10 }] } });
  expect(pin.ok()).toBe(true);
  const { snapshot_id: bookRef } = await pin.json();
  await page.route(worldChunk, (route) => route.abort());
  await page.goto(`/world?book_ref=${bookRef}`);
  await expect(page.getByRole("alert")).toContainText("World could not be loaded");
  await expect(page.getByRole("navigation", { name: "Pages" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Today", exact: true })).toHaveAttribute("href", `/?book_ref=${bookRef}`);
  await page.unroute(worldChunk);
  await page.getByRole("button", { name: "Reload page" }).click();
  await expect(page.getByRole("heading", { name: "World monitor", exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("book_ref")).toBe(bookRef);
});

test("a slow page shows a loading state without trapping navigation", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(/\/assets\/World-[^/]+\.js$/, async (route) => {
    await gate;
    await route.continue();
  });
  try {
    await page.goto("/world", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("status", { name: "Loading World" })).toBeVisible();
    // Native keyboard activation also verifies accessibility. WebKit's
    // frame-based pointer stability wait stalls with this intercepted script.
    const today = page.getByRole("link", { name: "Today", exact: true });
    await expect(today).toBeVisible();
    await today.focus();
    await today.press("Enter");
    await expect(page.getByRole("heading", { name: "Regime" })).toBeVisible();
    release();
    await page.waitForLoadState("networkidle");
    await expect(page.getByRole("heading", { name: "World monitor", exact: true })).toHaveCount(0);
  } finally {
    release();
  }
});

test("a failed candle chart download leaves market evidence and instrument controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route(/\/assets\/CandleChart-[^/]+\.js$/, (route) => route.abort());
  await page.route("**/api/instruments/*/candles*", (route) => route.fulfill({ json: {
    symbol: "SPX", days: 90,
    candles: [{ date: "2026-07-24", open: 100, high: 110, low: 90, close: 105, volume: 1000 }],
  } }));
  await page.goto("/");
  await expect(page.getByTestId("glance-SPX").getByRole("alert")).toContainText("Chart could not be loaded");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByRole("heading", { name: "Regime" })).toBeVisible();
  const instrument = page.getByTestId("glance-SPX").getByRole("button", { name: "S&P 500", exact: true });
  await instrument.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Chart could not be loaded");
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(instrument).toBeFocused();
  await page.getByRole("link", { name: "World", exact: true }).click();
  await expect(page.getByRole("heading", { name: "World monitor", exact: true })).toBeVisible();
});

test("deferred candle, series, and correlation charts render with one shared runtime", async ({ page }) => {
  const runtimeRequests: string[] = [];
  page.on("request", (request) => {
    if (/\/assets\/plotly-theme-[^/]+\.js$/.test(request.url())) runtimeRequests.push(request.url());
  });
  await page.route("**/api/instruments/*/candles*", (route) => route.fulfill({ json: {
    symbol: "SPX", days: 90,
    candles: [
      { date: "2026-07-23", open: 100, high: 110, low: 90, close: 105, volume: 1000 },
      { date: "2026-07-24", open: 105, high: 112, low: 100, close: 107, volume: 1200 },
    ],
  } }));
  await page.route("**/api/macro", (route) => route.fulfill({ json: {
    yields: { spread_2s10s: 0.01, series: {
      us10y: [{ date: "2026-07-23", value: 0.04 }, { date: "2026-07-24", value: 0.045 }],
      us2y: [{ date: "2026-07-23", value: 0.03 }, { date: "2026-07-24", value: 0.035 }],
    } },
  } }));
  await page.route("**/api/rotation", (route) => route.fulfill({ json: {
    universe: "sectors", symbols: ["SPY", "QQQ"], matrix: [[1, 0.8], [0.8, 1]],
    corr_window: 60, return_days: 5, returns: [{ symbol: "SPY", ret: 0.01 }, { symbol: "QQQ", ret: 0.02 }],
    anchor: null, other_side: null, as_of: "2026-07-24", missing: [],
  } }));
  await page.goto("/");
  await expect(page.getByTestId("candle-chart")).toHaveCount(4);
  for (const symbol of ["SPX", "VIX", "USO", "GLD"]) {
    await expect(page.getByTestId(`glance-${symbol}`).locator(".js-plotly-plot svg.main-svg").first()).toBeVisible();
  }
  await expect(page.getByTestId("glance-2s10s").locator(".js-plotly-plot svg.main-svg").first()).toBeVisible();
  await expect(page.getByTestId("corr-heatmap").locator("svg.main-svg").first()).toBeVisible();
  expect(runtimeRequests).toHaveLength(1);
});
