import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  outputDir: process.env.SRE_TEST_E2E_OUTPUT_DIR || "test-results",
  reporter: [["list"], ["json", { outputFile: process.env.SRE_TEST_E2E_JSON || "test-results/results.json" }]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    env: { VITE_AGENT_API_BASE_URL: "http://127.0.0.1:4173/mock-api" },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
