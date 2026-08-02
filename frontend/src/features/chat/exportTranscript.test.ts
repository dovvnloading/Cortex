import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import { exportTranscriptAsJson, exportTranscriptAsMarkdown, transcriptAsJson, transcriptAsMarkdown } from "./exportTranscript";

const chat: ChatResponse = {
  id: "thread-1",
  title: "Weekend plans",
  timestamp: "2026-01-01T00:00:00Z",
  revision: 2,
  messages: [
    { id: "m-1", role: "user", content: "What should I do this weekend?" },
    { id: "m-2", role: "assistant", content: "Consider a hike." },
  ],
};

describe("transcriptAsMarkdown", () => {
  it("renders each message as a labeled section separated by a rule", () => {
    const markdown = transcriptAsMarkdown(chat);
    expect(markdown).toBe(
      "### You\n\nWhat should I do this weekend?\n\n---\n\n### Cortex\n\nConsider a hike.",
    );
  });

  it("handles a chat with no messages", () => {
    expect(transcriptAsMarkdown({ ...chat, messages: [] })).toBe("");
  });
});

describe("transcriptAsJson", () => {
  it("round-trips the full chat structure", () => {
    expect(JSON.parse(transcriptAsJson(chat))).toEqual(chat);
  });
});

describe("export downloads", () => {
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.restoreAllMocks();
  });

  it("exportTranscriptAsMarkdown triggers a sanitized .md download", () => {
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
    const click = vi.fn();
    const anchor = document.createElement("a");
    vi.spyOn(anchor, "click").mockImplementation(click);
    vi.spyOn(document, "createElement").mockReturnValue(anchor);

    exportTranscriptAsMarkdown({ ...chat, title: "Weekend: plans?" });

    expect(anchor.download).toBe("Weekend_ plans_.md");
    expect(click).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock");
  });

  it("exportTranscriptAsJson triggers a .json download", () => {
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
    const click = vi.fn();
    const anchor = document.createElement("a");
    vi.spyOn(anchor, "click").mockImplementation(click);
    vi.spyOn(document, "createElement").mockReturnValue(anchor);

    exportTranscriptAsJson(chat);

    expect(anchor.download).toBe("Weekend plans.json");
    expect(click).toHaveBeenCalled();
  });
});
