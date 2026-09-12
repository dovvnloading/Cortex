import { describe, expect, it } from "vitest";

import { formatMessageTime } from "./messageTime";

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
