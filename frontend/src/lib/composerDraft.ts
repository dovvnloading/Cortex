const DRAFT_PREFIX = "cortex.composer.draft.";

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
