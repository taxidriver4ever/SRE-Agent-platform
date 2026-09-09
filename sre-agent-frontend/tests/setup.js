import { afterEach, beforeEach, vi } from "vitest";
import { config } from "@vue/test-utils";

config.global.stubs = { transition: false };

beforeEach(() => {
  localStorage.clear();
  window.location.hash = "#/services";
  window.scrollTo = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  window.requestAnimationFrame = (callback) => {
    callback(0);
    return 1;
  };
  globalThis.crypto.randomUUID = vi.fn(() => `uuid-${Math.random().toString(16).slice(2)}`);
});

afterEach(() => {
  vi.restoreAllMocks();
});
