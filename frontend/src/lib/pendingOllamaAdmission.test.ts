import { afterEach, describe, expect, it } from "vitest";
import {
  beginOllamaPullAdmission,
  clearOllamaPullAdmission,
  OLLAMA_PULL_ADMISSION_KEY,
  readPendingOllamaPullAdmission,
} from "./pendingOllamaAdmission";

afterEach(() => window.sessionStorage.clear());

describe("Ollama pull acceptance recovery", () => {
  it("replays the same request and retains the original model exactly", () => {
    const original = beginOllamaPullAdmission(" qwen3:8b ");
    expect(beginOllamaPullAdmission("qwen3:8b")).toEqual(original);
    expect(original.model).toBe("qwen3:8b");
    expect(original.requestId).toEqual(expect.any(String));
    expect(readPendingOllamaPullAdmission()).toEqual(original);

    clearOllamaPullAdmission("not-the-original-id");
    expect(readPendingOllamaPullAdmission()).toEqual(original);
    clearOllamaPullAdmission(original.requestId);
    expect(readPendingOllamaPullAdmission()).toBeNull();
  });

  it("does not overwrite a pending model pull with a different model", () => {
    const pending = beginOllamaPullAdmission("qwen3:8b");
    expect(() => beginOllamaPullAdmission("other:7b")).toThrow(/previous Ollama pull/i);
    expect(readPendingOllamaPullAdmission()).toEqual(pending);
  });

  it("rejects malformed or expired stored acceptance intents", () => {
    window.sessionStorage.setItem(OLLAMA_PULL_ADMISSION_KEY, JSON.stringify({
      model: "qwen3:8b", requestId: "not valid ID", createdAt: Date.now(),
    }));
    expect(readPendingOllamaPullAdmission()).toBeNull();

    window.sessionStorage.setItem(OLLAMA_PULL_ADMISSION_KEY, JSON.stringify({
      model: "qwen3:8b", requestId: "valid-id", createdAt: Date.now() - 25 * 60 * 60 * 1000,
    }));
    expect(readPendingOllamaPullAdmission()).toBeNull();
  });

  it("does not resurrect a cleared in-memory admission", () => {
    beginOllamaPullAdmission("qwen3:8b");
    window.sessionStorage.clear();
    expect(readPendingOllamaPullAdmission()).toBeNull();
  });
});
