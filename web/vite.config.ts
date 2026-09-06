/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const apiProxyTarget = process.env.QM_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

// Dev topology (design doc decision 5A): Vite proxies /api to FastAPI —
// zero CORS surface. Daily use: FastAPI serves the built assets itself.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: { "/api": apiProxyTarget },
  },
  build: {
    modulePreload: {
      // Keep initial HTML preloads, but let dynamic JS use native import().
      // WebKit can retain failed modulepreloads across reloads (bug 270357).
      // Vite still loads a dynamic page's associated CSS before rendering it.
      resolveDependencies: (_filename, dependencies, { hostType }) =>
        hostType === "html" ? dependencies : [],
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    // web/e2e/** holds Playwright specs (run via `bunx playwright test`, its
    // own runner) — exclude them here so Vitest's default *.spec.ts glob
    // doesn't try to collect them too (wave-2 Task 4: Playwright smoke).
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
