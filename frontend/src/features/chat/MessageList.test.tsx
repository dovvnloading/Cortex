import { createRef } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { MessageList, type MessageListHandle } from "./MessageList";

function makeMessages(count: number): ChatMessage[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `m-${index}`,
    role: index % 2 === 0 ? "user" : "assistant",
    content: `Message ${index}`,
  }));
}

describe("MessageList", () => {
  it("renders the plain scrollable transcript below the virtualization threshold", () => {
    const messages = makeMessages(5);
    render(
      <MessageList
        messages={messages}
        isStreaming={false}
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
      />,
    );

    expect(document.querySelector(".transcript")).not.toBeNull();
    expect(document.querySelector(".transcript-virtual")).toBeNull();
    for (const message of messages) {
      expect(screen.getByText(message.content)).toBeInTheDocument();
    }
  });

  it("switches to react-virtuoso at the virtualization threshold", async () => {
    // jsdom reports zero layout height, so react-virtuoso (correctly, given
    // that viewport) renders zero items here — actual content rendering for
    // the virtualized path is covered by e2e/virtualized-transcript.spec.ts
    // in a real browser. This test only proves the threshold-driven switch
    // to the react-virtuoso container itself happens.
    const messages = makeMessages(45);
    render(
      <MessageList
        messages={messages}
        isStreaming={false}
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(document.querySelector(".transcript-virtual")).not.toBeNull());
    expect(document.querySelector('[data-testid="virtuoso-scroller"]')).not.toBeNull();
  });

  it("renders trailingContent (the in-flight streaming bubble) inside the same scroll container, plain path", () => {
    render(
      <MessageList
        messages={makeMessages(3)}
        isStreaming
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
        trailingContent={<div data-testid="pending-bubble">Streaming…</div>}
      />,
    );

    const transcript = document.querySelector(".transcript");
    const bubble = screen.getByTestId("pending-bubble");
    expect(transcript?.contains(bubble)).toBe(true);
  });

  it("renders trailingContent inside the virtualized container via the Footer slot", async () => {
    render(
      <MessageList
        messages={makeMessages(45)}
        isStreaming
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
        trailingContent={<div data-testid="pending-bubble">Streaming…</div>}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("pending-bubble")).toBeInTheDocument());
  });

  it("reports near-end scroll state via onNearEndChange on the plain path", () => {
    const onNearEndChange = vi.fn();
    render(
      <MessageList
        messages={makeMessages(3)}
        isStreaming={false}
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={onNearEndChange}
      />,
    );

    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(transcript, "clientHeight", { value: 400, configurable: true });
    transcript.scrollTop = 650; // 1000 - 650 - 400 = -50 < 80 → near end
    transcript.dispatchEvent(new Event("scroll"));
    expect(onNearEndChange).toHaveBeenCalledWith(true);

    transcript.scrollTop = 0; // 1000 - 0 - 400 = 600, not near end
    transcript.dispatchEvent(new Event("scroll"));
    expect(onNearEndChange).toHaveBeenCalledWith(false);
  });

  it("exposes scrollToBottom() via the imperative handle on the plain path", () => {
    const ref = createRef<MessageListHandle>();
    render(
      <MessageList
        ref={ref}
        messages={makeMessages(3)}
        isStreaming={false}
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
      />,
    );

    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 900, configurable: true });
    transcript.scrollTop = 0;

    ref.current?.scrollToBottom();
    expect(transcript.scrollTop).toBe(900);
  });
});
