import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { buildCrashRecord, describeCrash, LAST_CRASH_KEY, readLastCrash, recordCrash } from "./crashLog";

describe("crashLog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("records when, which part of the app and what kind of error, and nothing else", () => {
    const record = buildCrashRecord(new TypeError("x"), "", "chat", new Date("2026-09-29T10:00:00Z"));

    expect(record).toEqual({ time: "2026-09-29T10:00:00.000Z", scope: "chat", name: "TypeError", componentPath: "" });
  });

  it("flattens and bounds the scope the app passes in", () => {
    const record = buildCrashRecord(new Error("x"), "", `first\nsecond\u0000third ${"x".repeat(200)}`);

    expect(record.scope.startsWith("first second third ")).toBe(true);
    expect(record.scope).not.toContain("\n");
    expect(record.scope).not.toContain("\u0000");
    expect(record.scope.length).toBeLessThanOrEqual(40);
    expect(buildCrashRecord(new Error("x"), "", "  ").scope).toBe("app");
  });

  describe("an error's own text", () => {
    // Nothing decides, from the outside, that an error message is free of
    // conversation text: the app itself puts file names into some, and a
    // message can be built from anything running code was holding. So none is
    // copied, whatever kind of error carries it.
    const HOSTILE = "PRIVATE-PROMPT-TEXT: what my doctor said about the lawsuit";
    class SummaryError extends Error {
      constructor(message: string) {
        super(message);
        this.name = "PromptSummaryError";
      }
    }
    const hostileErrors = (): unknown[] => [
      new Error(HOSTILE),
      new TypeError(HOSTILE),
      new RangeError(HOSTILE),
      new ApiError(500, HOSTILE),
      new SummaryError(HOSTILE),
      Object.assign(new Error("failed"), { name: HOSTILE }),
      Object.assign(new Error("failed"), { detail: HOSTILE, response: { text: HOSTILE } }),
    ];

    it("is never copied into the record or the text a person pastes", () => {
      for (const error of hostileErrors()) {
        const record = buildCrashRecord(error, "    at Card (bundle.js:1:1)", "chat", new Date("2026-09-29T10:00:00Z"));

        expect(JSON.stringify(record)).not.toContain("PRIVATE-PROMPT-TEXT");
        expect(describeCrash(record)).not.toContain("PRIVATE-PROMPT-TEXT");
      }
    });

    it("is not stored either", () => {
      for (const error of hostileErrors()) {
        recordCrash(error, "    at Card (bundle.js:1:1)", "chat");

        expect(window.sessionStorage.getItem(LAST_CRASH_KEY) ?? "").not.toContain("PRIVATE-PROMPT-TEXT");
      }
    });

    it("leaves only the error's class name, and only when it is one of a known few", () => {
      const nameOf = (error: unknown) => buildCrashRecord(error, "", "app").name;

      expect(nameOf(new TypeError("x"))).toBe("TypeError");
      expect(nameOf(new RangeError("x"))).toBe("RangeError");
      expect(nameOf(new ApiError(500, "x"))).toBe("ApiError");
      // A name is read from running code too, so an unknown one is not trusted.
      expect(nameOf(new SummaryError("x"))).toBe("Error");
      expect(nameOf(Object.assign(new Error("x"), { name: HOSTILE }))).toBe("Error");
    });
  });

  it("reduces the component stack to component names, innermost first, without locations", () => {
    const stack = [
      "    at Card (http://localhost:5173/src/features/chat/Card.tsx?t=17:12:3)",
      "    at MessageList (http://localhost:5173/src/features/chat/MessageList.tsx:1:1)",
      "    at Suspense (<anonymous>)",
      "    at <anonymous>:1:1",
      "Gecko@http://localhost:5173/src/Gecko.tsx:2:2",
      "    at PRIVATE-PROMPT-TEXT looks like prose (not a frame)",
    ].join("\n");

    const record = buildCrashRecord(new Error("x"), stack, "chat");

    expect(record.componentPath).toBe("Card > MessageList > Suspense > Gecko");
    expect(JSON.stringify(record)).not.toMatch(/localhost|\.tsx|:12:3|PRIVATE/);
  });

  it("bounds the component path to 25 names and skips a name that is too long", () => {
    const frames = Array.from({ length: 100 }, (_, index) => `    at Component${index} (bundle.js:${index}:1)\u0007`);
    frames.splice(1, 0, `    at ${"L".repeat(200)} (bundle.js:1:1)`);

    const record = buildCrashRecord(new Error("boom"), frames.join("\r\n"), "app");

    const names = record.componentPath.split(" > ");
    expect(names).toHaveLength(25);
    expect(names.slice(0, 3)).toEqual(["Component0", "Component1", "Component2"]);
    expect(record.componentPath).not.toContain("\u0007");
    expect(record.componentPath).not.toContain("LLLL");
    expect(record.componentPath.length).toBeLessThan(500);
  });

  it("has no component path when the stack is missing or holds no frames", () => {
    expect(buildCrashRecord(new Error("x"), undefined, "app").componentPath).toBe("");
    expect(buildCrashRecord(new Error("x"), null, "app").componentPath).toBe("");
    expect(buildCrashRecord(new Error("x"), "\n\n   \nnot a frame at all", "app").componentPath).toBe("");
  });

  it("never turns an arbitrary thrown value into text", () => {
    const secret = { prompt: "PRIVATE-PROMPT-TEXT", toString: () => "PRIVATE-PROMPT-TEXT" };

    for (const thrown of ["PRIVATE-PROMPT-TEXT", secret, 42, null, undefined]) {
      const record = buildCrashRecord(thrown, undefined, "app");
      expect(JSON.stringify(record)).not.toContain("PRIVATE-PROMPT-TEXT");
      expect(record.name).toBe("Error");
      expect(record).not.toHaveProperty("message");
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
      componentPath: "ChatPage > Suspense > App",
    });

    expect(text.split("\n")).toEqual([
      "Cortex UI crash",
      "Time: 2026-09-29T10:00:00.000Z",
      "View: chat",
      "Error type: TypeError",
      "Components, innermost first: ChatPage > Suspense > App",
    ]);
  });

  it("leaves the component line out of the pasted text when there is no path", () => {
    const text = describeCrash({ time: "2026-09-29T10:00:00.000Z", scope: "app", name: "Error", componentPath: "" });

    expect(text.split("\n")).toEqual([
      "Cortex UI crash",
      "Time: 2026-09-29T10:00:00.000Z",
      "View: app",
      "Error type: Error",
    ]);
  });
});
