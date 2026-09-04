// The app boots against the fixed synthetic cache, the shell renders, and
// sidebar navigation reaches the Lab bench. Deeper per-page behavior remains
// covered by the Vitest/MSW suites.
import { expect, test } from "@playwright/test";

test("loads synthetic Today data and navigates to Lab", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("QUANTMIND", { exact: true })).toBeVisible();
  await expect(page.getByText("SPY").first()).toBeVisible();
  await expect(page.getByTestId("topbar-asof")).toHaveText("data as of 2026-07-24");

  await page.getByRole("link", { name: "Lab", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Model", exact: true })).toBeVisible();
});

test("keeps navigation and the setup workflow inside a phone viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/book/setup");

  await expect(page.getByRole("heading", { name: "Finish local setup" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Setup", exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  ).toBe(true);

  await page.getByRole("link", { name: "Today", exact: true }).click();
  await expect(page.getByText("SPY").first()).toBeVisible();
  await expect(page.getByTestId("sync-now")).toBeHidden();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  ).toBe(true);
});

test("uses phones as a read-only setup companion and tablets as an authoring surface", async ({ page }) => {
  await page.route("**/api/setup/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        overall: "needs_attention",
        api: { status: "ready", version: "0.4.0.0" },
        broker: { status: "connected", provider: "IBKR", mode: "paper", error: null },
        market_data: {
          status: "ready",
          symbols: 1,
          ready_symbols: 1,
          missing_symbols: [],
          stale_symbols: [],
          corrupt_symbols: [],
          series: 4,
          as_of: "2026-09-04",
          age_days: 0,
        },
        macro_data: {
          status: "ready",
          required_series: 4,
          ready_series: 4,
          missing_series: [],
          stale_series: [],
          corrupt_series: [],
          as_of: "2026-09-04",
          age_days: 0,
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
        next_action: "pin_book",
      }),
    })
  );

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/book/setup");
  await expect(page.getByText("Open QuantMind on a screen at least 768 × 600")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pin current book" })).toBeHidden();

  await page.setViewportSize({ width: 768, height: 1024 });
  await expect(page.getByText("Open QuantMind on a screen at least 768 × 600")).toBeHidden();
  await expect(page.getByRole("button", { name: "Pin current book" })).toBeVisible();
});

test("hides analysis authoring controls throughout the phone companion", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  for (const { path, action } of [
    { path: "/risk", action: "Run Monte Carlo" },
    { path: "/hedge", action: "Run" },
    { path: "/whatif", action: "Compute" },
    { path: "/lab", action: "Fit" },
  ]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: action, exact: true })).toBeHidden();
  }
});

test("keeps every analysis route inside a tablet viewport", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });

  for (const path of ["/portfolio", "/risk", "/hedge", "/whatif", "/macro", "/lab", "/book/setup"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      `${path} overflowed the viewport`
    ).toBe(true);
  }
});

test("uses the available analytical canvas on a wide monitor", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1440 });
  await page.goto("/risk");
  await page.waitForLoadState("networkidle");

  const canvasWidth = await page.locator("main > div").first().evaluate(
    (element) => element.getBoundingClientRect().width,
  );
  expect(canvasWidth).toBeGreaterThan(2200);
});
