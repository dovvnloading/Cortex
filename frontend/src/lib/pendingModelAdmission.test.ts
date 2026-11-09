import { afterEach, describe, expect, it } from "vitest";
import {
  beginGGUFAdmission,
  clearGGUFAdmission,
  GGUF_ADMISSION_KEY,
  readPendingGGUFAdmission,
} from "./pendingModelAdmission";

const request = {
  source: "huggingface" as const,
  repo_id: "owner/repo",
  filename: "model.Q4_K_M.gguf",
};

afterEach(() => window.sessionStorage.clear());

describe("GGUF acceptance recovery", () => {
  it("keeps the same request ID and original model/folder snapshots on replay", () => {
    const first = beginGGUFAdmission(request, "gguf:original.gguf", "C:/original");
    const retry = beginGGUFAdmission({ ...request }, "gguf:newly-selected.gguf", "C:/new");
    expect(retry).toEqual(first);
    expect(first.payload.request_id).toEqual(expect.any(String));
    expect(first.selectedChatModelAtStart).toBe("gguf:original.gguf");
    expect(first.ggufDirectoryAtStart).toBe("C:/original");

    expect(readPendingGGUFAdmission()).toEqual(first);
    clearGGUFAdmission("a different request id");
    expect(readPendingGGUFAdmission()).toEqual(first);
    clearGGUFAdmission(first.payload.request_id);
    expect(readPendingGGUFAdmission()).toBeNull();
  });

  it("does not replace a still-unconfirmed transfer ID with another download", () => {
    const first = beginGGUFAdmission(request, null, null);
    expect(() => beginGGUFAdmission({ ...request, filename: "different.gguf" }, null, null))
      .toThrow(/previous model download has not been confirmed/i);
    expect(readPendingGGUFAdmission()).toEqual(first);
  });

  it("rejects an invalid remembered pending request instead of replaying it", () => {
    window.sessionStorage.setItem(GGUF_ADMISSION_KEY, JSON.stringify({
      payload: { source: "url", request_id: "" },
      selectedChatModelAtStart: null,
      ggufDirectoryAtStart: null,
      createdAt: Date.now(),
    }));
    expect(readPendingGGUFAdmission()).toBeNull();
    expect(window.sessionStorage.getItem(GGUF_ADMISSION_KEY)).toBeNull();
  });

  it("does not resurrect in-memory state after session storage is deliberately cleared", () => {
    beginGGUFAdmission(request, null, null);
    window.sessionStorage.clear();
    expect(readPendingGGUFAdmission()).toBeNull();
  });
});
