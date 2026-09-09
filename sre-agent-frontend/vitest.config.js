import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.js"],
    include: ["tests/**/*.spec.js"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "html"],
      reportsDirectory: process.env.SRE_TEST_COVERAGE_DIR || "coverage",
      include: ["src/**/*.{js,vue}"],
      exclude: ["src/main.js"],
    },
  },
});
