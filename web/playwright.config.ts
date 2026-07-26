// Playwright smoke config (Task 4, wave-2 ops). Boots the real FastAPI
// backend (reading the developer's local data/ cache — renders-from-cache,
// no network needed) plus the Vite dev server, and runs ONE smoke spec
// against them. `reuseExistingServer: true` so a dev already running
// `bun run dev` / `uv run python -m quantmind.api.main` isn't duplicated.
import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

const WEB_DIR = fileURLToPath(new URL(".", import.meta.url));
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    // "localhost" (not 127.0.0.1) — Vite's default dev-server host resolves
    // to ::1 on this machine; matching it here is what makes
    // reuseExistingServer actually detect an already-running `vite`.
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python -m quantmind.api.main",
      cwd: REPO_ROOT,
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: "bun run dev",
      cwd: WEB_DIR,
      url: "http://localhost:5173",
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
});
