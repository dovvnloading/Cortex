import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../../../contracts/cortex-api";

// react-markdown is where the cost is: every render of it runs a full
// parse -> transform -> render pipeline. Count real invocations, by the source
// text they were handed, and pass straight through to the real component.
const parsed = vi.hoisted(() => ({ sources: [] as string[] }));

vi.mock("react-markdown", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-markdown")>();
  return {
    ...actual,
    default: (options: Parameters<typeof actual.default>[0]) => {
      parsed.sources.push(String(options.children));
      return actual.default(options);
    },
  };
});

// MessageCard formats its timestamp on every render, so counting those calls
// shows whether the card itself (not just its Markdown) rendered again.
const cardRenders = vi.hoisted(() => ({ count: 0 }));

vi.mock("../../lib/messageTime", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/messageTime")>();
  return {
    ...actual,
    formatMessageTime: (timestamp?: string | null) => {
      cardRenders.count += 1;
      return actual.formatMessageTime(timestamp);
    },
  };
});

import { MessageList } from "./MessageList";

const assistant = (id: string, content: string): ChatMessage => ({ id, role: "assistant", content });
const user = (id: string, content: string): ChatMessage => ({ id, role: "user", content });

function parsesOf(source: string): number {
  return parsed.sources.filter((candidate) => candidate === source).length;
}

const noop = () => undefined;

describe("MessageList render cost", () => {
  // ChatPage hands the list stable callbacks (useCallback), so the tests do too.
  const onRegenerate = vi.fn();
  const onFork = vi.fn();

  const props = (messages: ChatMessage[], trailingContent: React.ReactNode) => ({
    messages,
    isStreaming: true,
    finalAssistantId: "a2",
    busy: true,
    forkingMessageId: null,
    onRegenerate,
    onFork,
    onNearEndChange: noop,
    trailingContent,
  });

  it("does not re-parse any message when only the trailing content changes", () => {
    parsed.sources.length = 0;
    const messages = [user("u1", "Question"), assistant("a1", "First **answer**"), user("u2", "Follow-up"), assistant("a2", "Second `answer`")];
    const { rerender } = render(<MessageList {...props(messages, <span>frame 0</span>)} />);
    expect(parsesOf("First **answer**")).toBe(1);
    expect(parsesOf("Second `answer`")).toBe(1);

    // A streamed frame: same messages, new trailing bubble, sixty times.
    for (let frame = 1; frame <= 60; frame += 1) {
      rerender(<MessageList {...props(messages, <span>frame {frame}</span>)} />);
    }

    expect(parsesOf("First **answer**")).toBe(1);
    expect(parsesOf("Second `answer`")).toBe(1);
  });

  it("does not re-render the cards themselves when only the trailing content changes", () => {
    cardRenders.count = 0;
    const stamp = "2026-01-01T10:00:00Z";
    const messages = [
      { ...user("u1", "Question"), timestamp: stamp },
      { ...assistant("a1", "Answer one"), timestamp: stamp },
      { ...assistant("a2", "Answer two"), timestamp: stamp },
    ];
    const { rerender } = render(<MessageList {...props(messages, <span>frame 0</span>)} />);
    const afterMount = cardRenders.count;
    expect(afterMount).toBeGreaterThanOrEqual(messages.length);

    for (let frame = 1; frame <= 30; frame += 1) {
      rerender(<MessageList {...props(messages, <span>frame {frame}</span>)} />);
    }

    expect(cardRenders.count).toBe(afterMount);
  });

  it("re-parses only the message whose content actually changed", () => {
    parsed.sources.length = 0;
    const first = assistant("a1", "Unchanged answer");
    const second = assistant("a2", "Original wording");
    const { rerender } = render(<MessageList {...props([first, second], null)} />);

    rerender(<MessageList {...props([first, assistant("a2", "Edited wording")], null)} />);

    expect(parsesOf("Unchanged answer")).toBe(1);
    expect(parsesOf("Original wording")).toBe(1);
    expect(parsesOf("Edited wording")).toBe(1);
  });

  it("still re-renders a card whose busy or final state flips", () => {
    parsed.sources.length = 0;
    const messages = [assistant("a1", "Only answer")];
    const base = { ...props(messages, null), finalAssistantId: "a1" };
    const { rerender, getByRole } = render(<MessageList {...base} busy />);
    expect(getByRole("button", { name: "Regenerate response" })).toBeDisabled();

    rerender(<MessageList {...base} busy={false} />);

    expect(getByRole("button", { name: "Regenerate response" })).toBeEnabled();
  });

  it("calls back with the message and its position", () => {
    onRegenerate.mockClear();
    onFork.mockClear();
    const messages = [user("u1", "Question"), assistant("a1", "Answer")];
    const { getByRole } = render(<MessageList {...props(messages, null)} busy={false} finalAssistantId="a1" />);

    getByRole("button", { name: "Regenerate response" }).click();
    getByRole("button", { name: "Fork chat from this message" }).click();

    expect(onRegenerate).toHaveBeenCalledWith(messages[1], 1);
    expect(onFork).toHaveBeenCalledWith(messages[1]);
  });
});
