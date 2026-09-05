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
  await page.goto(`/portfolio?book_ref=${ref}`);
  await page.getByRole("link", { name: "World", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/world\\?book_ref=${ref}`));
  await expect(page.getByText(new RegExp(`Pinned book ${ref}`))).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  await page.getByRole("option", { name: "Macro", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/macro\\?book_ref=${ref}`));
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
