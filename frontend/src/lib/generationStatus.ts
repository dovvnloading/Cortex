/**
 * Shown in place of live progress once part of the answer was missed: the
 * backend keeps only a bounded tail of a running job's events, so a reader that
 * was away long enough is served from partway through.
 */
export const GENERATION_GAP_NOTICE = "Part of this answer was missed while you were away. The full answer will appear when it is saved.";

/** Convert backend progress into text that is safe to show in the UI. */
export function humanizeGenerationStatus(status: string): string {
  const normalized = status.trim();
  if (!normalized || normalized === "Ready" || /^[A-Z][A-Z0-9_]*$/.test(normalized)) {
    return "Generating a response...";
  }
  return normalized;
}
