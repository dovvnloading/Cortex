import { afterEach, describe, expect, it } from "vitest";
import {
  clearPendingGenerationAdmission,
  readPendingGenerationAdmission,
  storePendingGenerationAdmission,
  type PendingGenerationAdmission,
} from "./pendingGenerationAdmission";

const valid = (): PendingGenerationAdmission => ({
  requestId: "request-1",
  operation: "generate",
  threadId: null,
  baseRevision: 0,
  prompt: "A saved question",
  attachments: [],
  createdAt: Date.now(),
});

describe("pending generation admission recovery", () => {
  afterEach(() => {
    clearPendingGenerationAdmission();
    window.sessionStorage.clear();
  });

  it("preserves the complete request until explicitly reconciled", () => {
    const intent = valid();
    storePendingGenerationAdmission(intent);
    expect(readPendingGenerationAdmission()).toEqual(intent);
    clearPendingGenerationAdmission();
    expect(readPendingGenerationAdmission()).toBeNull();
  });

  it("rejects malformed, missing and expired records", () => {
    const old = { ...valid(), createdAt: Date.now() - 25 * 60 * 60 * 1000 };
    storePendingGenerationAdmission(old);
    expect(readPendingGenerationAdmission()).toBeNull();
    window.sessionStorage.setItem("cortex.pending.generation-admission", JSON.stringify({
      ...valid(),
      requestId: "../invalid",
    }));
    expect(readPendingGenerationAdmission()).toBeNull();
    window.sessionStorage.setItem("cortex.pending.generation-admission", "{");
    expect(readPendingGenerationAdmission()).toBeNull();
  });
});
