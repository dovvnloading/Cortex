/**
 * Parse a stored message timestamp into epoch milliseconds.
 *
 * ECMAScript parses a zone-less date-time as *local* time, so a timestamp
 * stored without an offset renders shifted by the viewer's UTC offset. The
 * backend now always writes one; treating a missing offset as UTC keeps any
 * other producer -- or a row written by an older build -- from reintroducing
 * the shift.
 */
function parseMessageTimestamp(value?: string | null): number | null {
  if (!value) return null;
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  const timestamp = Date.parse(normalized);
  return Number.isNaN(timestamp) ? null : timestamp;
}

const CLOCK_TIME: Intl.DateTimeFormatOptions = { hour: "numeric", minute: "2-digit" };

/**
 * Format a stored message timestamp for display, in the viewer's locale and
 * time zone. A message from today shows only the clock time; an earlier one in
 * the same year adds the month and day, and one from an earlier year adds the
 * year, so a reopened three-week-old thread does not repeat one time of day on
 * every turn with no way to tell which day it was.
 *
 * `now` is a parameter so the "same day" boundary can be tested.
 */
export function formatMessageTime(value?: string | null, now: Date = new Date()): string | null {
  const timestamp = parseMessageTimestamp(value);
  if (timestamp === null) return null;
  const moment = new Date(timestamp);
  const options: Intl.DateTimeFormatOptions = { ...CLOCK_TIME };
  if (moment.getFullYear() !== now.getFullYear()) {
    options.year = "numeric";
    options.month = "short";
    options.day = "numeric";
  } else if (moment.getMonth() !== now.getMonth() || moment.getDate() !== now.getDate()) {
    options.month = "short";
    options.day = "numeric";
  }
  return new Intl.DateTimeFormat(undefined, options).format(timestamp);
}

/** The full date and time, for a `title` (and so a tooltip) on the compact form above. */
export function formatMessageTimeTitle(value?: string | null): string | null {
  const timestamp = parseMessageTimestamp(value);
  if (timestamp === null) return null;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "medium" }).format(timestamp);
}
