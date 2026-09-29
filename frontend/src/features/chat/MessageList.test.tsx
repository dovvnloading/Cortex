import { createRef, forwardRef, useEffect, useImperativeHandle } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
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

  it("restores a reader's plain-scroll position when crossing the threshold", async () => {
    vi.resetModules();
    const scrollTo = vi.fn();
    vi.doMock("react-virtuoso", () => ({
      Virtuoso: forwardRef<VirtuosoHandle, VirtuosoProps<ChatMessage, unknown>>(function MockVirtuoso(_props, ref) {
        useImperativeHandle(ref, () => ({
          scrollTo,
          scrollToIndex: () => {},
          scrollBy: () => {},
          autoscrollToBottom: () => {},
          scrollIntoView: () => {},
          getState: () => { throw new Error("not implemented in this test double"); },
        }));
        return <div data-testid="virtuoso-scroller" />;
      }),
    }));
    const { MessageList: MockedMessageList } = await import("./MessageList");
    const messages = makeMessages(39);
    const { rerender } = render(
      <MockedMessageList
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
    const transcript = document.querySelector(".transcript") as HTMLElement;
    Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 1200 });
    Object.defineProperty(transcript, "clientHeight", { configurable: true, value: 400 });
    transcript.scrollTop = 260;
    fireEvent.scroll(transcript);

    rerender(
      <MockedMessageList
        messages={makeMessages(40)}
        isStreaming={false}
        finalAssistantId={null}
        busy={false}
        forkingMessageId={null}
        onRegenerate={vi.fn()}
        onFork={vi.fn()}
        onNearEndChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 260, behavior: "auto" }));
    vi.doUnmock("react-virtuoso");
    vi.resetModules();
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

describe("MessageList scrolling", () => {
  type Captured = { props: VirtuosoProps<ChatMessage, unknown> | null; scrollTo: Mock<(location: ScrollToOptions) => void> };

  /** The list with Virtuoso replaced by a stand-in that records the props it was given and the scrolls it was asked for. */
  async function loadWithVirtuoso() {
    vi.resetModules();
    const captured: Captured = { props: null, scrollTo: vi.fn<(location: ScrollToOptions) => void>() };
    vi.doMock("react-virtuoso", () => ({
      Virtuoso: forwardRef<VirtuosoHandle, VirtuosoProps<ChatMessage, unknown>>(function MockVirtuoso(props, ref) {
        captured.props = props;
        useImperativeHandle(ref, () => ({
          scrollTo: captured.scrollTo,
          scrollToIndex: () => {},
          scrollBy: () => {},
          autoscrollToBottom: () => {},
          scrollIntoView: () => {},
          getState: () => { throw new Error("not implemented in this test double"); },
        }));
        return <div data-testid="virtuoso-scroller" />;
      }),
    }));
    const { MessageList: MockedMessageList } = await import("./MessageList");
    return { MockedMessageList, captured };
  }

  function listProps(messages: ChatMessage[], isStreaming = false) {
    return {
      messages,
      isStreaming,
      finalAssistantId: null,
      busy: false,
      forkingMessageId: null,
      onRegenerate: vi.fn(),
      onFork: vi.fn(),
      onNearEndChange: vi.fn(),
    };
  }

  function preferReducedMotion(reduce: boolean) {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: vi.fn((query: string) => ({
        matches: reduce && query.includes("prefers-reduced-motion"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  }

  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
    vi.doUnmock("react-virtuoso");
    vi.resetModules();
  });

  it("uses the same bottom band on the virtualized transcript as on the plain one", async () => {
    const { MockedMessageList, captured } = await loadWithVirtuoso();
    render(<MockedMessageList {...listProps(makeMessages(45))} />);

    // Virtuoso's own default is 4px; a reader 60px from the end was "at the
    // bottom" on a short chat and "away" on a long one.
    expect(captured.props?.atBottomThreshold).toBe(80);

    const onNearEndChange = vi.fn();
    vi.resetModules();
    const { MessageList: PlainList } = await import("./MessageList");
    // Below the threshold the plain path is used; find the same band there.
    render(<PlainList {...listProps(makeMessages(3))} onNearEndChange={onNearEndChange} />);
    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(transcript, "clientHeight", { value: 400, configurable: true });

    transcript.scrollTop = 521; // 79px from the end
    fireEvent.scroll(transcript);
    expect(onNearEndChange).toHaveBeenLastCalledWith(true);
    transcript.scrollTop = 519; // 81px from the end
    fireEvent.scroll(transcript);
    expect(onNearEndChange).toHaveBeenLastCalledWith(false);
  });

  it("follows streamed output smoothly on the virtualized transcript, and not at all when idle", async () => {
    const { MockedMessageList, captured } = await loadWithVirtuoso();
    const { rerender } = render(<MockedMessageList {...listProps(makeMessages(45), true)} />);
    expect(captured.props?.followOutput).toBe("smooth");

    rerender(<MockedMessageList {...listProps(makeMessages(45), false)} />);
    expect(captured.props?.followOutput).toBe(false);
  });

  it("does not animate the follow when the person asked for reduced motion", async () => {
    preferReducedMotion(true);
    const { MockedMessageList, captured } = await loadWithVirtuoso();
    render(<MockedMessageList {...listProps(makeMessages(45), true)} />);

    // Virtuoso animates in script, which the stylesheet's reduced-motion rule cannot reach.
    expect(captured.props?.followOutput).toBe("auto");
  });

  it("scrolls the plain transcript instantly by default, so a streamed frame never restarts an animation", async () => {
    const { MockedMessageList } = await loadWithVirtuoso();
    const ref = createRef<MessageListHandle>();
    render(<MockedMessageList ref={ref} {...listProps(makeMessages(3), true)} />);
    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 900, configurable: true });
    const scrollTo = vi.fn();
    transcript.scrollTo = scrollTo;

    ref.current?.scrollToBottom();

    // .transcript has `scroll-behavior: smooth`; only an explicit behavior overrides it.
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith({ top: 900, behavior: "instant" });
  });

  it("animates the explicit jump, unless reduced motion was asked for", async () => {
    const { MockedMessageList } = await loadWithVirtuoso();
    const ref = createRef<MessageListHandle>();
    render(<MockedMessageList ref={ref} {...listProps(makeMessages(3))} />);
    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 900, configurable: true });
    const scrollTo = vi.fn();
    transcript.scrollTo = scrollTo;

    ref.current?.scrollToBottom("smooth");
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 900, behavior: "smooth" });
  });

  it("makes the explicit jump instant when reduced motion was asked for", async () => {
    preferReducedMotion(true);
    const { MockedMessageList } = await loadWithVirtuoso();
    const ref = createRef<MessageListHandle>();
    render(<MockedMessageList ref={ref} {...listProps(makeMessages(3))} />);
    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperty(transcript, "scrollHeight", { value: 900, configurable: true });
    const scrollTo = vi.fn();
    transcript.scrollTo = scrollTo;

    ref.current?.scrollToBottom("smooth");

    expect(scrollTo).toHaveBeenCalledWith({ top: 900, behavior: "instant" });
  });

  it("gives the virtualized transcript the same instant, smooth, and reduced-motion rules", async () => {
    const { MockedMessageList, captured } = await loadWithVirtuoso();
    const ref = createRef<MessageListHandle>();
    render(<MockedMessageList ref={ref} {...listProps(makeMessages(45), true)} />);

    ref.current?.scrollToBottom();
    expect(captured.scrollTo).toHaveBeenLastCalledWith({ top: Number.MAX_SAFE_INTEGER, behavior: "instant" });
    ref.current?.scrollToBottom("smooth");
    expect(captured.scrollTo).toHaveBeenLastCalledWith({ top: Number.MAX_SAFE_INTEGER, behavior: "smooth" });
  });

  it("makes the virtualized jump instant under reduced motion", async () => {
    preferReducedMotion(true);
    const { MockedMessageList, captured } = await loadWithVirtuoso();
    const ref = createRef<MessageListHandle>();
    render(<MockedMessageList ref={ref} {...listProps(makeMessages(45))} />);

    ref.current?.scrollToBottom("smooth");

    expect(captured.scrollTo).toHaveBeenCalledWith({ top: Number.MAX_SAFE_INTEGER, behavior: "instant" });
  });
});
