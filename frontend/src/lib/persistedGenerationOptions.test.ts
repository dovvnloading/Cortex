import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PERSISTED_GENERATION_OPTIONS_KEY,
  prunePersistedGenerationOptions,
  readPersistedGenerationOptions,
  writePersistedGenerationOptions,
} from "./persistedGenerationOptions";

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("durable per-thread generation options", () => {
  it("restores numeric chat overrides across a fresh Zustand store import", async () => {
    vi.resetModules();
    const { useChatStore } = await import("../stores/useChatStore");
    useChatStore.getState().setThreadOptions("thread-1", {
      temperature: 1, top_p: 0.95, top_k: 80, num_ctx: 16384, seed: 42,
    });
    expect(readPersistedGenerationOptions()["thread-1"]).toMatchObject({
      temperature: 1, num_ctx: 16384, seed: 42,
    });

    vi.resetModules(); // Equivalent to a fresh JS bundle reading localStorage on startup.
    const fresh = await import("../stores/useChatStore");
    expect(fresh.useChatStore.getState().generationOptionsByThread["thread-1"]).toEqual({
      temperature: 1, top_p: 0.95, top_k: 80, num_ctx: 16384, seed: 42,
    });
  });

  it("does not persist options for an unsaved new thread", () => {
    writePersistedGenerationOptions({
      new: { temperature: 1 },
      "saved-thread": { temperature: 0.2 },
    });
    expect(readPersistedGenerationOptions()).toEqual({ "saved-thread": { temperature: 0.2 } });
  });

  it("removes a reset override without resurrecting it on another load", () => {
    writePersistedGenerationOptions({ "saved-thread": { temperature: 0.2 } });
    writePersistedGenerationOptions({});
    expect(window.localStorage.getItem(PERSISTED_GENERATION_OPTIONS_KEY)).toBeNull();
    expect(readPersistedGenerationOptions()).toEqual({});
  });

  it("prunes preferences for server-deleted chats while retaining saved ones", () => {
    writePersistedGenerationOptions({
      "chat-a": { seed: 5 },
      "chat-b": { temperature: 0.8 },
    });
    prunePersistedGenerationOptions(["chat-b"]);
    expect(readPersistedGenerationOptions()).toEqual({ "chat-b": { temperature: 0.8 } });
  });

  it("fails closed on corrupted, oversized, and invalid numeric values", () => {
    window.localStorage.setItem(PERSISTED_GENERATION_OPTIONS_KEY, "{broken");
    expect(readPersistedGenerationOptions()).toEqual({});

    window.localStorage.setItem(PERSISTED_GENERATION_OPTIONS_KEY, "x".repeat(32001));
    expect(readPersistedGenerationOptions()).toEqual({});

    window.localStorage.setItem(PERSISTED_GENERATION_OPTIONS_KEY, JSON.stringify({
      version: 1,
      options: {
        "good-chat": { top_k: 100, num_ctx: 8192 },
        "bad-chat": { num_ctx: 999999 },
        "also-bad": { seed: "a secret string, not an option" },
      },
    }));
    expect(readPersistedGenerationOptions()).toEqual({
      "good-chat": { top_k: 100, num_ctx: 8192 },
    });
  });

  it("caps local preference storage without including prompts, chat content, or credentials", () => {
    const many = Object.fromEntries(
      Array.from({ length: 130 }, (_, index) => [`chat-${index}`, { temperature: 1 }]),
    );
    writePersistedGenerationOptions(many);
    const restored = readPersistedGenerationOptions();
    expect(Object.keys(restored)).toHaveLength(100);
    expect(restored["chat-0"]).toBeUndefined();
    expect(restored["chat-129"]).toEqual({ temperature: 1 });
    const stored = window.localStorage.getItem(PERSISTED_GENERATION_OPTIONS_KEY)!;
    expect(stored).not.toContain("user_input");
    expect(stored).not.toContain("session_token");
  });
});
