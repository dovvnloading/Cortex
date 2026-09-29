import { afterEach, describe, expect, it, vi } from "vitest";
import { buildCrashRecord, describeCrash, LAST_CRASH_KEY, readLastCrash, recordCrash } from "./crashLog";

describe("crashLog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("flattens and bounds the one field that comes from running code", () => {
    const long = `first line\nsecond\u0000line ${"x".repeat(1000)}`;
    const record = buildCrashRecord(new TypeError(long), "", "chat", new Date("2026-09-29T10:00:00Z"));

    expect(record.time).toBe("2026-09-29T10:00:00.000Z");
    expect(record.name).toBe("TypeError");
    expect(record.message.startsWith("first line second line ")).toBe(true);
    expect(record.message).not.toContain("\n");
    expect(record.message).not.toContain("\u0000");
    expect(record.message.length).toBeLessThanOrEqual(300);
  });

  it("bounds the component stack by lines and by size, and drops control characters", () => {
    const stack = Array.from({ length: 100 }, (_, index) => `    at Component${index} (bundle.js:${index}:1)\u0007`).join("\n");

    const record = buildCrashRecord(new Error("boom"), stack, "app");

    expect(record.componentStack.split("\n")).toHaveLength(25);
    expect(record.componentStack).not.toContain("\u0007");
    expect(record.componentStack.length).toBeLessThanOrEqual(2000);
  });

  it("never turns an arbitrary thrown value into text", () => {
    const secret = { prompt: "PRIVATE-PROMPT-TEXT", toString: () => "PRIVATE-PROMPT-TEXT" };

    for (const thrown of ["PRIVATE-PROMPT-TEXT", secret, 42, null, undefined]) {
      const record = buildCrashRecord(thrown, undefined, "app");
      expect(JSON.stringify(record)).not.toContain("PRIVATE-PROMPT-TEXT");
      expect(record.name).toBe("Error");
      expect(record.message).toBe("");
    }
  });

  it("does not read a cause, props or anything else off the error", () => {
    const error = Object.assign(new Error("failed"), { cause: "PRIVATE-PROMPT-TEXT", props: { text: "PRIVATE-PROMPT-TEXT" } });

    expect(JSON.stringify(buildCrashRecord(error, undefined, "app"))).not.toContain("PRIVATE-PROMPT-TEXT");
  });

  it("writes to session storage only, and reads back what it wrote", () => {
    const local = vi.spyOn(Storage.prototype, "setItem");

    const written = recordCrash(new Error("boom"), "    at View (bundle.js:1:1)", "settings");

    expect(readLastCrash()).toEqual(written);
    expect(window.sessionStorage.getItem(LAST_CRASH_KEY)).toContain("settings");
    expect(local).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(LAST_CRASH_KEY)).toBeNull();
  });

  it("survives storage that refuses the write or the read", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    expect(() => recordCrash(new Error("boom"), "", "app")).not.toThrow();

    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Denied", "SecurityError");
    });
    expect(readLastCrash()).toBeNull();
  });

  it("ignores a stored value that is not a crash record", () => {
    window.sessionStorage.setItem(LAST_CRASH_KEY, "not json");
    expect(readLastCrash()).toBeNull();
    window.sessionStorage.setItem(LAST_CRASH_KEY, JSON.stringify({ time: 1, scope: "app" }));
    expect(readLastCrash()).toBeNull();
  });

  it("describes a crash as text a person can paste", () => {
    const text = describeCrash({
      time: "2026-09-29T10:00:00.000Z",
      scope: "chat",
      name: "TypeError",
      message: "x is undefined",
      componentStack: "    at ChatPage (bundle.js:1:1)",
    });

    expect(text.split("\n")).toEqual([
      "Cortex UI crash",
      "Time: 2026-09-29T10:00:00.000Z",
      "View: chat",
      "Error: TypeError: x is undefined",
      "Component stack:",
      "    at ChatPage (bundle.js:1:1)",
    ]);
  });
});
