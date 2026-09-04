import { expect, test } from "vitest";
import { PAGES } from "../shell/Shell";

test("the contextual book setup flow stays out of global navigation", () => {
  expect(PAGES).toContainEqual({ path: "/book/setup", label: "Setup", navigation: false });
  expect(PAGES.filter((page) => page.navigation).map((page) => page.label)).not.toContain("Setup");
});
