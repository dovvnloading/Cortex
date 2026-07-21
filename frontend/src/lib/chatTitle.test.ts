import { describe, expect, it } from "vitest";
import { displayChatTitle } from "./chatTitle";

describe("displayChatTitle", () => {
  it("renders legacy Markdown-wrapped model titles as plain text", () => {
    expect(displayChatTitle("**AI Purpose Explained**")).toBe("AI Purpose Explained");
    expect(displayChatTitle("### [Cortex planning](https://example.test)")).toBe("Cortex planning");
  });

  it("keeps normal titles intact and supplies a readable fallback", () => {
    expect(displayChatTitle("Cortex: local-first chat")).toBe("Cortex: local-first chat");
    expect(displayChatTitle("  ")).toBe("Untitled chat");
  });
});
