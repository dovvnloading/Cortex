import type { ChatAttachment } from "../../../contracts/cortex-api";

const DRAFT_PREFIX = "cortex.composer.draft.";
const ATTACHMENT_PREFIX = "cortex.composer.attachments.";

/**
 * Keeps unfinished messages local to the browser session and scoped to one
 * conversation. These drafts are deliberately not sent to the backend.
 */
export function readComposerDraft(threadId: string | null): string {
  try {
    return window.sessionStorage.getItem(composerDraftKey(threadId)) ?? "";
  } catch {
    return "";
  }
}

export function writeComposerDraft(threadId: string | null, value: string): void {
  try {
    const key = composerDraftKey(threadId);
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // Session storage is an optional resilience layer. The controlled input
    // remains fully usable if a browser denies storage access.
  }
}

export function composerDraftKey(threadId: string | null): string {
  return `${DRAFT_PREFIX}${threadId ?? "new"}`;
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
  return `${ATTACHMENT_PREFIX}${threadId ?? "new"}`;
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
