/**
 * Format a stored message timestamp for display.
 *
 * ECMAScript parses a zone-less date-time as *local* time, so a timestamp
 * stored without an offset renders shifted by the viewer's UTC offset. The
 * backend now always writes one; treating a missing offset as UTC keeps any
 * other producer -- or a row written by an older build -- from reintroducing
 * the shift.
 */
export function formatMessageTime(value?: string | null): string | null {
  if (!value) return null;
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  const timestamp = Date.parse(normalized);
  if (Number.isNaN(timestamp)) return null;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(timestamp);
}
