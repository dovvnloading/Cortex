import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { useChatStore } from "../../stores/useChatStore";
import { MessageCard } from "./MessageCard";

const NOTE = "Couldn't translate this answer; showing the original.";

function renderCard(message: ChatMessage, onSaveMemory?: (memo: string) => Promise<boolean>) {
  return render(
    <MessageCard message={message} index={0} isFinalAssistant busy={false} onRegenerate={vi.fn()} onFork={vi.fn()} onSaveMemory={onSaveMemory} forking={false} />,
  );
}

describe("MessageCard", () => {
  afterEach(() => {
    useChatStore.setState({ untranslatedMessageIds: {}, proposedMemoriesByMessage: {} });
  });

  it("notes quietly when an answer was left untranslated", () => {
    renderCard({ id: "assistant-1", role: "assistant", content: "Hello there." });
    expect(screen.queryByText(NOTE)).toBeNull();

    act(() => useChatStore.getState().markUntranslated("assistant-1"));

    expect(screen.getByRole("note")).toHaveTextContent(NOTE);
    // The answer itself is still shown, untouched.
    expect(screen.getByText("Hello there.")).toBeInTheDocument();
  });

  it("does not note other messages", () => {
    useChatStore.getState().markUntranslated("assistant-1");
    renderCard({ id: "assistant-2", role: "assistant", content: "Another answer." });
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it("offers the memories the model suggested under its own answer only", () => {
    useChatStore.getState().setProposedMemories("assistant-1", ["User likes tea."]);
    renderCard({ id: "assistant-1", role: "assistant", content: "Noted." }, vi.fn());

    expect(screen.getByRole("region", { name: "Cortex suggests remembering" })).toHaveTextContent("User likes tea.");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("offers nothing under a message that has no suggestions", () => {
    useChatStore.getState().setProposedMemories("assistant-1", ["User likes tea."]);
    renderCard({ id: "assistant-2", role: "assistant", content: "Another answer." }, vi.fn());

    expect(screen.queryByRole("region", { name: "Cortex suggests remembering" })).toBeNull();
  });

  it("never offers a suggestion under a user's own message", () => {
    useChatStore.getState().setProposedMemories("user-1", ["User likes tea."]);
    renderCard({ id: "user-1", role: "user", content: "I like tea." }, vi.fn());

    expect(screen.queryByRole("region", { name: "Cortex suggests remembering" })).toBeNull();
  });

  it("does not show suggestions it has no way to save", () => {
    useChatStore.getState().setProposedMemories("assistant-1", ["User likes tea."]);
    renderCard({ id: "assistant-1", role: "assistant", content: "Noted." });

    expect(screen.queryByRole("region", { name: "Cortex suggests remembering" })).toBeNull();
  });

  it("shows the date on an older message and always carries the full time in a title", () => {
    renderCard({ id: "assistant-1", role: "assistant", content: "Old answer.", timestamp: "2020-01-05T10:00:00Z" });

    const time = document.querySelector("time");
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute("datetime", "2020-01-05T10:00:00Z");
    // Long ago: the year is part of the visible text, not only the tooltip.
    expect(time?.textContent).toContain("2020");
    expect(time?.getAttribute("title")).toContain("2020");
  });

  it("shows only the clock time for a message from today", () => {
    const now = new Date();
    renderCard({ id: "assistant-1", role: "assistant", content: "New answer.", timestamp: now.toISOString() });

    const time = document.querySelector("time");
    expect(time?.textContent).not.toContain(String(now.getFullYear()));
    expect(time?.getAttribute("title")).toContain(String(now.getFullYear()));
  });

  it("drops the suggestions from the card once they have all been decided", () => {
    useChatStore.getState().setProposedMemories("assistant-1", ["User likes tea."]);
    renderCard({ id: "assistant-1", role: "assistant", content: "Noted." }, vi.fn());

    act(() => useChatStore.getState().dismissProposedMemory("assistant-1", "User likes tea."));

    expect(screen.queryByRole("region", { name: "Cortex suggests remembering" })).toBeNull();
    expect(screen.getByText("Noted.")).toBeInTheDocument();
  });
});
