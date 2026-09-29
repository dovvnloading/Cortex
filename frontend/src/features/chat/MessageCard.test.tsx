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

  it("drops the suggestions from the card once they have all been decided", () => {
    useChatStore.getState().setProposedMemories("assistant-1", ["User likes tea."]);
    renderCard({ id: "assistant-1", role: "assistant", content: "Noted." }, vi.fn());

    act(() => useChatStore.getState().dismissProposedMemory("assistant-1", "User likes tea."));

    expect(screen.queryByRole("region", { name: "Cortex suggests remembering" })).toBeNull();
    expect(screen.getByText("Noted.")).toBeInTheDocument();
  });
});
