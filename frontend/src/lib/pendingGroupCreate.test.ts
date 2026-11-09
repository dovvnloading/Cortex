import { afterEach, describe, expect, it } from "vitest";
import {
  beginGroupCreate,
  clearGroupCreate,
  PENDING_GROUP_CREATE_KEY,
  readPendingGroupCreate,
} from "./pendingGroupCreate";

afterEach(() => {
  window.sessionStorage.removeItem(PENDING_GROUP_CREATE_KEY);
});

describe("pending chat-group creation", () => {
  it("reuses the request key on retry and after re-reading tab storage", () => {
    const original = beginGroupCreate(" Research ");
    expect(original.name).toBe("Research");
    expect(original.requestId).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(readPendingGroupCreate()).toEqual(original);
    expect(beginGroupCreate("Research")).toEqual(original);
    expect(JSON.parse(window.sessionStorage.getItem(PENDING_GROUP_CREATE_KEY) ?? "null")).toEqual(original);
  });

  it("does not silently overwrite an unconfirmed create for a different folder", () => {
    const original = beginGroupCreate("Research");
    expect(() => beginGroupCreate("Planning")).toThrow(/previous group creation/);
    expect(readPendingGroupCreate()).toEqual(original);
    clearGroupCreate("wrong-id");
    expect(readPendingGroupCreate()).toEqual(original);
    clearGroupCreate(original.requestId);
    expect(readPendingGroupCreate()).toBeNull();
    expect(beginGroupCreate("Research").requestId).not.toBe(original.requestId);
  });

  it("rejects a corrupted or stale pending identity", () => {
    window.sessionStorage.setItem(PENDING_GROUP_CREATE_KEY, JSON.stringify({
      name: "Research", requestId: "bogus key", createdAt: Date.now(),
    }));
    expect(readPendingGroupCreate()).toBeNull();
    const next = beginGroupCreate("Research");
    expect(next.requestId).not.toBe("bogus key");
  });
});
