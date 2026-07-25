// One smoke spec (wave-2 Task 4): the app boots, the shell renders, and
// sidebar navigation reaches the Lab bench. Kept intentionally minimal —
// deeper per-page behavior is covered by the Vitest/MSW suites.
import { expect, test } from "@playwright/test";

test("loads Today with the wordmark and a tile, navigates to Lab", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("QUANTMIND")).toBeVisible();
  // At least one cached-universe tile symbol renders on the morning bench.
  await expect(page.getByText("SPY").first()).toBeVisible();

  await page.getByRole("link", { name: "Lab", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Model", exact: true })).toBeVisible();
});
