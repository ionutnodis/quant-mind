// Hermetic Playwright smoke config. Boots a deterministic synthetic FastAPI
// cache plus an isolated Vite port; it never reads a developer's holdings or
// reuses an already-running local dashboard.
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
    baseURL: "http://localhost:4173",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: [
    {
      command: "uv run python -m quantmind.testing.synthetic_e2e --port 8765",
      cwd: REPO_ROOT,
      url: "http://127.0.0.1:8765/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "bun run dev -- --host localhost --port 4173 --strictPort",
      cwd: WEB_DIR,
      env: { QM_API_PROXY_TARGET: "http://127.0.0.1:8765" },
      url: "http://localhost:4173",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
