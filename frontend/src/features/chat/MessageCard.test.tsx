import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { useChatStore } from "../../stores/useChatStore";
import { MessageCard } from "./MessageCard";

const NOTE = "Couldn't translate this answer; showing the original.";

function renderCard(message: ChatMessage) {
  return render(
    <MessageCard message={message} isFinalAssistant busy={false} onRegenerate={vi.fn()} onFork={vi.fn()} forking={false} />,
  );
}

describe("MessageCard", () => {
  afterEach(() => {
    useChatStore.setState({ untranslatedMessageIds: {} });
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
});
