import { afterEach, describe, expect, it, vi } from "vitest";
import { readPendingGenerationAdmission, storePendingGenerationAdmission } from "./pendingGenerationAdmission";
import { REQUEST_ID_PATTERN, createRequestId } from "./requestId";

describe("createRequestId", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("makes a valid id even without crypto.randomUUID (#696)", () => {
    // Shadow the prototype's method on this instance; deleting it restores it.
    Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
    vi.spyOn(Math, "random").mockReturnValue(0.123);
    try {
      const first = createRequestId();
      const second = createRequestId();
      expect(first).toMatch(REQUEST_ID_PATTERN);
      expect(first).not.toBe(second);
      // A pending admission keyed by it survives the round trip through storage.
      storePendingGenerationAdmission({
        requestId: first, operation: "generate", threadId: null, prompt: "Hello", attachments: [],
        createdAt: Date.now(),
      });
      expect(readPendingGenerationAdmission()?.requestId).toBe(first);
    } finally {
      delete (crypto as { randomUUID?: unknown }).randomUUID;
    }
  });

  it("uses crypto.randomUUID when it is there", () => {
    expect(createRequestId()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });
});
