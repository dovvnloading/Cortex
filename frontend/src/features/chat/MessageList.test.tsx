import { createRef, forwardRef, useEffect, useImperativeHandle } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { VirtuosoHandle, VirtuosoProps } from "react-virtuoso";
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

  it("updates the virtualized Footer content in place instead of remounting it on every change", async () => {
    // The real react-virtuoso only redraws its Footer slot in response to
    // its own internal layout/scroll signals, which jsdom's zero-height
    // environment never fires after mount -- so a plain rerender() can't
    // observe an update through the real library here. Swap in a minimal
    // stand-in that always re-invokes components.Footer on every render,
    // the way the real library does in a browser, so this test can isolate
    // and verify the actual contract MessageList relies on: components.Footer
    // must be read fresh each render (so content updates) while the
    // function's own identity stays stable (so React doesn't remount it).
    vi.resetModules();
    vi.doMock("react-virtuoso", () => ({
      Virtuoso: forwardRef<VirtuosoHandle, VirtuosoProps<ChatMessage, unknown>>(function MockVirtuoso(props, ref) {
        useImperativeHandle(ref, () => ({
          scrollToIndex: () => {},
          scrollTo: () => {},
          scrollBy: () => {},
          autoscrollToBottom: () => {},
          scrollIntoView: () => {},
          getState: () => { throw new Error("not implemented in this test double"); },
        }));
        const Footer = props.components?.Footer;
        return <div data-testid="virtuoso-scroller">{Footer ? <Footer context={undefined} /> : null}</div>;
      }),
    }));
    const { MessageList: MockedMessageList } = await import("./MessageList");

    const mountSpy = vi.fn();
    function Probe({ label }: { label: string }) {
      // Fires only on true mount (empty deps) -- a remount would call this
      // again; an in-place update of the same instance would not.
      useEffect(() => { mountSpy(); }, []);
      return <div data-testid="pending-bubble">{label}</div>;
    }

    const { rerender } = render(
      <MockedMessageList
        messages={makeMessages(45)}
        isStreaming
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
        trailingContent={<Probe label="Streaming…" />}
      />,
    );
    expect(screen.getByTestId("pending-bubble")).toHaveTextContent("Streaming…");
    expect(mountSpy).toHaveBeenCalledTimes(1);

    rerender(
      <MockedMessageList
        messages={makeMessages(45)}
        isStreaming
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
        trailingContent={<Probe label="Streaming… more text" />}
      />,
    );

    expect(screen.getByTestId("pending-bubble")).toHaveTextContent("Streaming… more text");
    expect(mountSpy).toHaveBeenCalledTimes(1);

    vi.doUnmock("react-virtuoso");
    vi.resetModules();
  });

  it("scrolls the virtualized transcript to its true bottom, not to the last message", async () => {
    // The streaming bubble is rendered in the Footer slot, below the final
    // item. scrollToIndex(last item) stops short of it, so a long transcript
    // would scroll away from the answer being typed. Assert the handle
    // targets the scroller's bottom instead.
    vi.resetModules();
    const scrollTo = vi.fn();
    const scrollToIndex = vi.fn();
    vi.doMock("react-virtuoso", () => ({
      Virtuoso: forwardRef<VirtuosoHandle, VirtuosoProps<ChatMessage, unknown>>(function MockVirtuoso(_props, ref) {
        useImperativeHandle(ref, () => ({
          scrollToIndex,
          scrollTo,
          scrollBy: () => {},
          autoscrollToBottom: () => {},
          scrollIntoView: () => {},
          getState: () => { throw new Error("not implemented in this test double"); },
        }));
        return <div data-testid="virtuoso-scroller" />;
      }),
    }));
    const { MessageList: MockedMessageList } = await import("./MessageList");

    const ref = createRef<MessageListHandle>();
    render(
      <MockedMessageList
        ref={ref}
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

    ref.current?.scrollToBottom();

    expect(scrollToIndex).not.toHaveBeenCalled();
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo.mock.calls[0][0]).toMatchObject({ top: Number.MAX_SAFE_INTEGER });

    vi.doUnmock("react-virtuoso");
    vi.resetModules();
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
