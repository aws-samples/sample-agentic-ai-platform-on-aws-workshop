import { defineConfig } from "@playwright/test";

/**
 * End-to-end click-through of the deployed (or local) UI through the real Cognito Hosted UI and platform API.
 *   BASE_URL=https://<cloudfront> npx playwright test            (default http://localhost:5173, the Vite dev server)
 * Persona passwords: E2E_ADMIN_PASSWORD / E2E_DEVELOPER_PASSWORD / E2E_APPROVER_PASSWORD, or read from the users secret
 * (AWS_PROFILE=workshop) by e2e/global-setup.ts. Screenshots land in SCREENSHOT_DIR (default ../.claude/docs/ui-screenshots).
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 15 * 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: process.env.E2E_REPORT ?? "test-results/e2e-report.json" }]],
  outputDir: "test-results/artifacts",
  use: {
    baseURL: (process.env.BASE_URL ?? "http://localhost:5173").replace(/\/+$/, ""),
    viewport: { width: 1440, height: 1000 },
    ignoreHTTPSErrors: true,
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
