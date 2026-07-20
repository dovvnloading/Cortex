/** Convert backend progress into text that is safe to show in the UI. */
export function humanizeGenerationStatus(status: string): string {
  const normalized = status.trim();
  if (!normalized || normalized === "Ready" || /^[A-Z][A-Z0-9_]*$/.test(normalized)) {
    return "Generating a response...";
  }
  return normalized;
}
