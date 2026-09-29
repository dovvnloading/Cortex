import { describe, expect, it } from "vitest";

import { formatMessageTime, formatMessageTimeTitle } from "./messageTime";

describe("formatMessageTime", () => {
  it("reads a timestamp without an offset as UTC, not local time", () => {
    // ECMAScript parses a zone-less date-time as local time, so a stored
    // timestamp with no offset rendered shifted by the viewer's UTC offset --
    // and the optimistic message the composer creates does carry a "Z", so the
    // footer visibly jumped once the chat reloaded.
    expect(formatMessageTime("2026-01-01T12:00:00")).toBe(
      formatMessageTime("2026-01-01T12:00:00Z"),
    );
  });

  it("leaves an explicit offset alone", () => {
    expect(formatMessageTime("2026-01-01T12:00:00+00:00")).toBe(
      formatMessageTime("2026-01-01T12:00:00Z"),
    );
    expect(formatMessageTime("2026-01-01T07:00:00-05:00")).toBe(
      formatMessageTime("2026-01-01T12:00:00Z"),
    );
  });

  it("returns null for missing or unparsable values", () => {
    expect(formatMessageTime(null)).toBeNull();
    expect(formatMessageTime(undefined)).toBeNull();
    expect(formatMessageTime("")).toBeNull();
    expect(formatMessageTime("not a date")).toBeNull();
  });
});

// Local-time constructors keep these independent of the machine's time zone:
// "same day" is decided in the viewer's zone, so the fixtures are built there.
const NOW = new Date(2026, 5, 25, 12, 0, 0);
const at = (year: number, month: number, day: number, hour: number, minute: number) =>
  new Date(year, month, day, hour, minute, 0).toISOString();
const monthName = (iso: string) => new Intl.DateTimeFormat(undefined, { month: "short" }).format(Date.parse(iso));
const clock = (iso: string) => new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(Date.parse(iso));

describe("formatMessageTime date context", () => {
  it("shows only the clock time for a message from today", () => {
    const today = at(2026, 5, 25, 8, 5);
    const shown = formatMessageTime(today, NOW);
    expect(shown).toBe(clock(today));
    expect(shown).not.toContain(monthName(today));
    expect(shown).not.toContain("2026");
  });

  it("adds the month and day for an earlier day this year, but not the year", () => {
    const tenDaysAgo = at(2026, 5, 15, 9, 30);
    const shown = formatMessageTime(tenDaysAgo, NOW);
    expect(shown).toContain(monthName(tenDaysAgo));
    expect(shown).toContain("15");
    expect(shown).toContain(clock(tenDaysAgo));
    expect(shown).not.toContain("2026");
  });

  it("adds the year for a message from an earlier year", () => {
    const lastYear = at(2025, 11, 31, 22, 45);
    const shown = formatMessageTime(lastYear, NOW);
    expect(shown).toContain("2025");
    expect(shown).toContain(monthName(lastYear));
    expect(shown).toContain(clock(lastYear));
  });

  it("treats last night as another day, not as today", () => {
    // Just after midnight the previous evening's messages are still "yesterday".
    const justAfterMidnight = new Date(2026, 5, 25, 0, 1, 0);
    const lastNight = at(2026, 5, 24, 23, 59);
    expect(formatMessageTime(lastNight, justAfterMidnight)).toContain(monthName(lastNight));
    expect(formatMessageTime(at(2026, 5, 25, 0, 0), justAfterMidnight)).not.toContain(monthName(lastNight));
  });

  it("does not need a date for the same day-of-month in another month", () => {
    const lastMonth = at(2026, 4, 25, 12, 0);
    expect(formatMessageTime(lastMonth, NOW)).toContain(monthName(lastMonth));
  });
});

describe("formatMessageTimeTitle", () => {
  it("spells out the full date and time", () => {
    const title = formatMessageTimeTitle(at(2026, 5, 15, 9, 30));
    expect(title).toContain("2026");
    expect(title).toContain("15");
    // Longer than the compact form: it carries the weekday and seconds too.
    expect(title!.length).toBeGreaterThan(formatMessageTime(at(2026, 5, 15, 9, 30), NOW)!.length);
  });

  it("agrees with the compact form on which instant it describes, including a missing offset", () => {
    expect(formatMessageTimeTitle("2026-01-01T12:00:00")).toBe(formatMessageTimeTitle("2026-01-01T12:00:00Z"));
  });

  it("returns null for missing or unparsable values", () => {
    expect(formatMessageTimeTitle(null)).toBeNull();
    expect(formatMessageTimeTitle(undefined)).toBeNull();
    expect(formatMessageTimeTitle("not a date")).toBeNull();
  });
});
