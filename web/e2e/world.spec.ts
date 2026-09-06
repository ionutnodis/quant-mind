import { expect, test } from "@playwright/test";

test("World route saves a local lens against the real API and keeps pinned context in navigation", async ({ page }) => {
  await page.goto("/world");
  await expect(page.getByRole("heading", { name: "World monitor" })).toBeVisible();
  await page.getByLabel("Watch symbols", { exact: true }).fill("NVDA, ASML");
  await page.getByLabel("Interests", { exact: true }).fill("semiconductors, energy");
  await page.getByLabel("Regions", { exact: true }).fill("Europe, US");
  await page.getByRole("button", { name: "Save lens" }).click();
  await expect(page.getByText("Lens saved.", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Watch symbols", { exact: true })).toHaveValue("NVDA, ASML");
  const pin = await page.request.post("/api/book/pin", { data: { positions: [{ symbol: "SPY", qty: 10 }] } });
  expect(pin.ok()).toBe(true);
  const { snapshot_id: ref } = await pin.json();
  const pinnedWorld = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/world" && url.searchParams.get("book_ref") === ref;
  });
  await page.getByLabel("Pinned book reference", { exact: true }).fill(ref);
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  const pinnedResponse = await pinnedWorld;
  expect(pinnedResponse.ok()).toBe(true);
  expect((await pinnedResponse.json()).context).toEqual({
    book_ref: ref, label: `Pinned book ${ref}`, symbols: ["SPY"],
  });
  await expect(page).toHaveURL(new RegExp(`/world\\?book_ref=${ref}`));
  await expect(page.getByText(`Pinned book ${ref} · SPY`, { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Portfolio", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/portfolio\\?book_ref=${ref}`));
  await page.getByRole("link", { name: "World", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/world\\?book_ref=${ref}`));
  await expect(page.getByText(new RegExp(`Pinned book ${ref}`))).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  await page.getByRole("option", { name: "Macro", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/macro\\?book_ref=${ref}`));
});

test("World presents the real API's validation details without losing the edited lens", async ({ page }) => {
  await page.goto("/world");
  const symbols = page.getByLabel("Watch symbols", { exact: true });
  await symbols.fill("NOT A SYMBOL");
  const validation = page.waitForResponse((response) => response.url().endsWith("/api/world/profile") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Save lens" }).click();
  expect((await validation).status()).toBe(422);
  await expect(page.getByRole("alert")).toContainText(/watch_symbols:.*ticker identifiers/i);
  await expect(page.getByRole("alert")).not.toContainText("[object Object]");
  await expect(symbols).toHaveValue("NOT A SYMBOL");
  await expect(symbols).toBeEnabled();
});

test("World reflows from phone through ultrawide without horizontal scrolling", async ({ page }) => {
  for (const width of [320, 390, 640, 768, 1440, 2560, 3440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/world");
    await expect(page.getByRole("heading", { name: "World monitor" })).toBeVisible();
    await expect(page.getByRole("button", { name: "My lens" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (width < 768) {
      await expect(page.locator(".world")).toHaveCSS("font-size", "16px");
      await expect(page.getByRole("button", { name: "Save lens" })).toBeHidden();
      await expect(page.getByRole("button", { name: "Refresh sources" })).toBeHidden();
    } else {
      await expect(page.getByRole("button", { name: "Save lens" })).toBeVisible();
    }
  }
  await page.setViewportSize({ width: 900, height: 500 });
  await expect(page.getByRole("button", { name: "Save lens" })).toBeHidden();
  await expect(page.locator(".world")).toHaveCSS("font-size", "16px");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("long event text and saved lens values wrap inside narrow viewports", async ({ page }) => {
  const title = "W".repeat(300);
  await page.route("**/api/world", async (route) => {
    const upstream = await route.fetch();
    const snapshot = await upstream.json();
    await route.fulfill({ json: {
      ...snapshot,
      profile: { watch_symbols: ["NVDA"], interests: ["I".repeat(100)], regions: ["R".repeat(100)] },
      items: [{
        id: "long-text", source_id: "fed", source_name: "Federal Reserve",
        title, summary: "S".repeat(500), url: "https://example.org/long-text",
        published_at: "2026-09-05T08:00:00Z", time_kind: "published",
        topics: [], regions: [], relevance: 15,
        reasons: ["Interest: " + "I".repeat(100)], matched_symbols: [],
      }],
    } });
  });
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/world");
    await expect(page.getByRole("heading", { name: title })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
});

test.describe("World coarse-pointer targets", () => {
  test.use({ hasTouch: true });

  for (const viewport of [{ width: 390, height: 844 }, { width: 834, height: 1112 }]) {
    test(`event and source links have real 44px touch boxes at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.route("**/api/world", async (route) => {
        const upstream = await route.fetch();
        const snapshot = await upstream.json();
        await route.fulfill({ json: {
          ...snapshot,
          items: ["Policy update", "W".repeat(300)].map((title, index) => ({
            id: `touch-${index}`, source_id: "fed", source_name: "Federal Reserve",
            title, summary: "A cached policy event.", url: `https://example.org/touch-${index}`,
            published_at: "2026-09-05T08:00:00Z", time_kind: "published",
            topics: [], regions: [], relevance: 0, reasons: [], matched_symbols: [],
          })),
        } });
      });
      await page.goto("/world");
      await expect(page.getByRole("link", { name: "Policy update", exact: true })).toBeVisible();
      expect(await page.evaluate(() => matchMedia("(any-pointer: coarse)").matches)).toBe(true);
      const links = await page.locator(".world a[href]").evaluateAll((anchors) => anchors.map((anchor) => {
        const box = anchor.getBoundingClientRect();
        return { text: anchor.textContent, width: box.width, height: box.height };
      }));
      expect(links.length).toBeGreaterThan(3);
      for (const link of links) {
        expect(link.width, `${link.text} width`).toBeGreaterThanOrEqual(44);
        expect(link.height, `${link.text} height`).toBeGreaterThanOrEqual(44);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      if (viewport.width < 768) await expect(page.getByRole("button", { name: "Save lens" })).toBeHidden();
      else await expect(page.getByRole("button", { name: "Save lens" })).toBeVisible();
    });
  }
});
