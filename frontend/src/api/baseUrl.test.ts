// @vitest-environment node
import { describe, expect, it } from "vitest";
import { normalizeApiBaseUrl } from "./baseUrl";

describe("normalizeApiBaseUrl", () => {
  it("uses the same-origin API path by default", () => {
    expect(normalizeApiBaseUrl(undefined, true)).toBe("/api/v1");
    expect(normalizeApiBaseUrl("/api/v1/", true)).toBe("/api/v1");
  });

  it("allows explicit loopback origins in production", () => {
    expect(normalizeApiBaseUrl("http://127.0.0.1:8765/api/v1", true)).toBe("http://127.0.0.1:8765/api/v1");
    expect(normalizeApiBaseUrl("https://localhost:9443/api/v1/", true)).toBe("https://localhost:9443/api/v1");
    expect(normalizeApiBaseUrl("http://[::1]:8765/api/v1", true)).toBe("http://[::1]:8765/api/v1");
  });

  it("rejects remote or ambiguous production targets", () => {
    for (const value of [
      "https://example.com/api/v1",
      "//example.com/api/v1",
      "/api/v1?forward=remote",
      "/api/v1#fragment",
      "http://user:password@127.0.0.1/api/v1",
      "http://127.0.0.1/api/v1?forward=remote",
      "file:///tmp/cortex",
      "",
    ]) {
      expect(() => normalizeApiBaseUrl(value, true)).toThrow();
    }
  });

  it("keeps remote endpoints a development-only choice", () => {
    expect(normalizeApiBaseUrl("https://example.com/api/v1", false)).toBe("https://example.com/api/v1");
    expect(() => normalizeApiBaseUrl("http://user:password@example.com/api/v1", false)).toThrow();
  });
});
