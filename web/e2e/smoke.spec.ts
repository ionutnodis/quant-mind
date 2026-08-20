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
