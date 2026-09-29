import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatAttachment } from "../../../contracts/cortex-api";
import {
  composerAttachmentKey,
  composerDraftKey,
  discardComposerDraft,
  MAX_PERSISTED_DRAFT_CHARS,
  pruneComposerDrafts,
  readComposerAttachments,
  readComposerDraft,
  writeComposerAttachments,
  writeComposerDraft,
} from "./composerDraft";

const attachment: ChatAttachment = {
  attachment_id: "att-1",
  filename: "notes.txt",
  mime_type: "text/plain",
  size: 12,
  sha256: "a".repeat(64),
  kind: "document",
  expires_at: "2099-01-01T00:00:00Z",
};

/** A page restart: session storage is gone, local storage is what is left. */
const restart = () => window.sessionStorage.clear();

/** Make one of the two storages refuse writes (or reads), leaving the other alone. */
function breakStorage(which: "local" | "session", method: "setItem" | "getItem" | "removeItem" | "key") {
  const original = Storage.prototype[method] as (...args: unknown[]) => unknown;
  const target = which === "local" ? window.localStorage : window.sessionStorage;
  return vi.spyOn(Storage.prototype, method).mockImplementation(function (this: Storage, ...args: unknown[]) {
    if (this === target) throw new DOMException("storage denied", "SecurityError");
    return original.apply(this, args);
  } as never);
}

describe("composer drafts", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("keeps the storage keys the drafts have always used", () => {
    expect(composerDraftKey(null)).toBe("cortex.composer.draft.new");
    expect(composerDraftKey("thread-a")).toBe("cortex.composer.draft.thread-a");
    expect(composerAttachmentKey("thread-a")).toBe("cortex.composer.attachments.thread-a");
  });

  it("survives a restart, scoped to its own conversation", () => {
    writeComposerDraft("thread-a", "half a thought");
    writeComposerDraft("thread-b", "another one");
    writeComposerDraft(null, "a brand new chat");

    restart();

    expect(readComposerDraft("thread-a")).toBe("half a thought");
    expect(readComposerDraft("thread-b")).toBe("another one");
    expect(readComposerDraft(null)).toBe("a brand new chat");
    expect(readComposerDraft("thread-c")).toBe("");
  });

  it("reads back what was last written within a session", () => {
    writeComposerDraft("thread-a", "first");
    writeComposerDraft("thread-a", "second");

    expect(readComposerDraft("thread-a")).toBe("second");
    expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBe("second");
  });

  it("is gone from both storages once it is cleared, as sending does", () => {
    writeComposerDraft("thread-a", "sent it");
    writeComposerDraft("thread-a", "");

    expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBeNull();
    expect(window.sessionStorage.getItem(composerDraftKey("thread-a"))).toBeNull();
    restart();
    expect(readComposerDraft("thread-a")).toBe("");
  });

  describe("size limit", () => {
    it("persists a draft at the limit and keeps a longer one for the session only", () => {
      const atLimit = "x".repeat(MAX_PERSISTED_DRAFT_CHARS);
      const overLimit = "y".repeat(MAX_PERSISTED_DRAFT_CHARS + 1);

      writeComposerDraft("thread-a", atLimit);
      writeComposerDraft("thread-b", overLimit);

      expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBe(atLimit);
      expect(window.localStorage.getItem(composerDraftKey("thread-b"))).toBeNull();
      // Still there for a reload of the page...
      expect(readComposerDraft("thread-b")).toBe(overLimit);
      // ...but not for a restart.
      restart();
      expect(readComposerDraft("thread-a")).toBe(atLimit);
      expect(readComposerDraft("thread-b")).toBe("");
    });

    it("does not leave an older short copy behind to be restored over a draft that outgrew the limit", () => {
      writeComposerDraft("thread-a", "short");
      writeComposerDraft("thread-a", "z".repeat(MAX_PERSISTED_DRAFT_CHARS + 1));

      expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBeNull();
      restart();
      expect(readComposerDraft("thread-a")).toBe("");
    });
  });

  describe("when storage refuses", () => {
    it("still keeps the draft for the session when local storage is denied", () => {
      breakStorage("local", "setItem");

      expect(() => writeComposerDraft("thread-a", "typed anyway")).not.toThrow();

      expect(readComposerDraft("thread-a")).toBe("typed anyway");
    });

    it("falls back to the local copy when session storage cannot be read", () => {
      writeComposerDraft("thread-a", "kept locally");
      breakStorage("session", "getItem");

      expect(readComposerDraft("thread-a")).toBe("kept locally");
    });

    it("returns an empty draft, and does not throw, when nothing can be read or written", () => {
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
      vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });

      expect(() => writeComposerDraft("thread-a", "text")).not.toThrow();
      expect(() => writeComposerDraft("thread-a", "")).not.toThrow();
      expect(readComposerDraft("thread-a")).toBe("");
      expect(() => discardComposerDraft("thread-a")).not.toThrow();
    });
  });

  describe("attachments", () => {
    it("keeps attachment metadata for the session only, never in local storage", () => {
      writeComposerAttachments("thread-a", [attachment]);
      writeComposerDraft("thread-a", "see the file");

      expect(readComposerAttachments("thread-a")).toEqual([attachment]);
      expect(Object.keys(window.localStorage).filter((key) => key.includes("attachments"))).toEqual([]);
      // The only thing local storage holds is the text.
      expect(Object.entries(window.localStorage)).toEqual([[composerDraftKey("thread-a"), "see the file"]]);

      restart();
      expect(readComposerAttachments("thread-a")).toEqual([]);
      expect(readComposerDraft("thread-a")).toBe("see the file");
    });

    it("stores no file content, only the fields that describe the upload", () => {
      writeComposerAttachments("thread-a", [attachment]);

      const stored = JSON.parse(window.sessionStorage.getItem(composerAttachmentKey("thread-a")) ?? "[]") as Record<string, unknown>[];
      expect(Object.keys(stored[0]).sort()).toEqual(["attachment_id", "expires_at", "filename", "kind", "mime_type", "sha256", "size"]);
    });

    it("ignores malformed stored attachments", () => {
      window.sessionStorage.setItem(composerAttachmentKey("thread-a"), JSON.stringify([attachment, { attachment_id: 7 }, null]));
      expect(readComposerAttachments("thread-a")).toEqual([attachment]);

      window.sessionStorage.setItem(composerAttachmentKey("thread-a"), "not json");
      expect(readComposerAttachments("thread-a")).toEqual([]);
    });
  });

  describe("pruning drafts of conversations that no longer exist", () => {
    it("removes drafts and attachment metadata for missing chats and keeps the rest", () => {
      writeComposerDraft("kept", "keep me");
      writeComposerDraft("gone", "orphaned text");
      writeComposerDraft(null, "the new-chat draft");
      writeComposerAttachments("gone", [attachment]);
      writeComposerAttachments("kept", [attachment]);
      window.localStorage.setItem("cortex.theme", "dark");

      pruneComposerDrafts(["kept", "another"]);

      expect(window.localStorage.getItem(composerDraftKey("gone"))).toBeNull();
      expect(window.sessionStorage.getItem(composerDraftKey("gone"))).toBeNull();
      expect(window.sessionStorage.getItem(composerAttachmentKey("gone"))).toBeNull();
      expect(readComposerDraft("kept")).toBe("keep me");
      expect(readComposerAttachments("kept")).toEqual([attachment]);
      expect(readComposerDraft(null)).toBe("the new-chat draft");
      expect(window.localStorage.getItem("cortex.theme")).toBe("dark");
    });

    it("prunes drafts left in local storage by an earlier run", () => {
      writeComposerDraft("old-chat", "typed last week");
      restart();

      pruneComposerDrafts(["current-chat"]);

      expect(window.localStorage.getItem(composerDraftKey("old-chat"))).toBeNull();
      expect(readComposerDraft("old-chat")).toBe("");
    });

    it("removes every conversation's draft when the backend has none, but never the new-chat one", () => {
      writeComposerDraft("a", "1");
      writeComposerDraft("b", "2");
      writeComposerDraft(null, "3");

      pruneComposerDrafts([]);

      expect(Object.keys(window.localStorage)).toEqual([composerDraftKey(null)]);
    });

    it("does not throw when storage cannot be listed or edited", () => {
      writeComposerDraft("a", "1");
      breakStorage("local", "key");
      breakStorage("session", "removeItem");

      expect(() => pruneComposerDrafts([])).not.toThrow();
    });
  });

  it("forgets everything kept for a deleted conversation", () => {
    writeComposerDraft("thread-a", "text");
    writeComposerAttachments("thread-a", [attachment]);
    writeComposerDraft("thread-b", "other");

    discardComposerDraft("thread-a");

    expect(readComposerDraft("thread-a")).toBe("");
    expect(readComposerAttachments("thread-a")).toEqual([]);
    expect(readComposerDraft("thread-b")).toBe("other");
  });
});
