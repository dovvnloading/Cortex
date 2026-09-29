import type { ChatAttachment } from "../../../contracts/cortex-api";

const DRAFT_PREFIX = "cortex.composer.draft.";
const ATTACHMENT_PREFIX = "cortex.composer.attachments.";
const NEW_THREAD_SUFFIX = "new";

/**
 * Longest draft kept across restarts, in characters. The composer accepts up to
 * 100,000, and a browser's local storage is a few megabytes shared by
 * everything else the page keeps. A longer draft still survives a reload of the
 * page (session storage has no such limit here), just not a restart.
 */
export const MAX_PERSISTED_DRAFT_CHARS = 20_000;

/**
 * Keeps unfinished messages and scopes each to one conversation. These drafts
 * are deliberately not sent to the backend.
 *
 * The text is written to both storages: `localStorage` so it survives the app
 * being closed, `sessionStorage` so it survives a reload even when the draft is
 * too long for the former. Reading prefers the session copy, which is never
 * older than the local one. Attachments are another matter: they are staged
 * uploads that expire, so only their metadata is kept, for the session only,
 * and never any file bytes.
 */
export function readComposerDraft(threadId: string | null): string {
  const key = composerDraftKey(threadId);
  return readItem("session", key) ?? readItem("local", key) ?? "";
}

export function writeComposerDraft(threadId: string | null, value: string): void {
  const key = composerDraftKey(threadId);
  if (!value) {
    removeItem("session", key);
    removeItem("local", key);
    return;
  }
  writeItem("session", key, value);
  // A draft that no longer fits must not leave an older, shorter copy behind to
  // be restored in its place.
  if (value.length <= MAX_PERSISTED_DRAFT_CHARS) writeItem("local", key, value);
  else removeItem("local", key);
}

export function composerDraftKey(threadId: string | null): string {
  return `${DRAFT_PREFIX}${threadId ?? NEW_THREAD_SUFFIX}`;
}

/** Forget everything kept for one conversation, e.g. because it was deleted. */
export function discardComposerDraft(threadId: string): void {
  removeItem("session", composerDraftKey(threadId));
  removeItem("local", composerDraftKey(threadId));
  removeItem("session", composerAttachmentKey(threadId));
}

/**
 * Drop drafts of conversations that no longer exist, so text typed into a chat
 * that was deleted (in this or an earlier run) does not accumulate in storage.
 * Call it only with the complete list the backend just returned. The new-thread
 * draft belongs to no conversation and is always kept.
 */
export function pruneComposerDrafts(existingThreadIds: Iterable<string>): void {
  const existing = new Set(existingThreadIds);
  for (const kind of ["local", "session"] as const) {
    for (const key of listKeys(kind)) {
      const suffix = key.startsWith(DRAFT_PREFIX)
        ? key.slice(DRAFT_PREFIX.length)
        : key.startsWith(ATTACHMENT_PREFIX)
          ? key.slice(ATTACHMENT_PREFIX.length)
          : null;
      if (suffix === null || suffix === NEW_THREAD_SUFFIX || existing.has(suffix)) continue;
      removeItem(kind, key);
    }
  }
}

export function readComposerAttachments(threadId: string | null): ChatAttachment[] {
  try {
    const raw = window.sessionStorage.getItem(composerAttachmentKey(threadId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isChatAttachment) : [];
  } catch {
    return [];
  }
}

export function writeComposerAttachments(threadId: string | null, value: readonly ChatAttachment[]): void {
  try {
    const key = composerAttachmentKey(threadId);
    if (value.length) window.sessionStorage.setItem(key, JSON.stringify(value));
    else window.sessionStorage.removeItem(key);
  } catch {
    // Attachment bytes never live in session storage; metadata persistence is
    // optional and the in-memory controlled state remains authoritative.
  }
}

export function composerAttachmentKey(threadId: string | null): string {
  return `${ATTACHMENT_PREFIX}${threadId ?? NEW_THREAD_SUFFIX}`;
}

type StorageKind = "local" | "session";

// Storage is an optional resilience layer, and a browser may deny it outright
// (blocked site data, a private window), throw when it is full, or hand back
// nothing. The controlled input stays fully usable whatever happens here, so
// every access is guarded and a failure is simply a draft that is not kept.
function storageFor(kind: StorageKind): Storage {
  return kind === "local" ? window.localStorage : window.sessionStorage;
}

function readItem(kind: StorageKind, key: string): string | null {
  try {
    return storageFor(kind).getItem(key);
  } catch {
    return null;
  }
}

function writeItem(kind: StorageKind, key: string, value: string): void {
  try {
    storageFor(kind).setItem(key, value);
  } catch {
    // Denied or over quota: keep going without persistence.
  }
}

function removeItem(kind: StorageKind, key: string): void {
  try {
    storageFor(kind).removeItem(key);
  } catch {
    // Nothing useful to do; the entry is at worst pruned on a later launch.
  }
}

function listKeys(kind: StorageKind): string[] {
  try {
    const storage = storageFor(kind);
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null) keys.push(key);
    }
    return keys;
  } catch {
    return [];
  }
}

function isChatAttachment(value: unknown): value is ChatAttachment {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<ChatAttachment>;
  return typeof item.attachment_id === "string"
    && typeof item.filename === "string"
    && typeof item.mime_type === "string"
    && typeof item.size === "number"
    && typeof item.sha256 === "string"
    && (item.kind === "image" || item.kind === "document")
    && typeof item.expires_at === "string";
}
