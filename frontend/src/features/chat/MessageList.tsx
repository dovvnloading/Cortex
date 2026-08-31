import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { MessageCard } from "./MessageCard";

const VIRTUALIZE_THRESHOLD = 40;

export type MessageListHandle = {
  scrollToBottom: () => void;
};

type Props = {
  messages: ChatMessage[];
  isStreaming: boolean;
  finalAssistantId: string | null;
  busy: boolean;
  forkingMessageId: string | null;
  onRegenerate: (message: ChatMessage, index: number) => void;
  onFork: (message: ChatMessage) => void;
  onNearEndChange: (isNearEnd: boolean) => void;
  /** The in-flight streaming bubble, rendered inside the same scroll container so it participates in auto-scroll. */
  trailingContent?: ReactNode;
};

/**
 * Below VIRTUALIZE_THRESHOLD, this renders the exact same plain scrollable
 * div the transcript always has — same className, same DOM shape, so every
 * existing test/e2e fixture (all well under the threshold) sees no change.
 * Only larger transcripts switch to react-virtuoso, which owns its own
 * scroll container; scrollToBottom()/onNearEndChange() abstract over which
 * container is actually in play so the caller doesn't need to know.
 */
export const MessageList = forwardRef<MessageListHandle, Props>(function MessageList(
  { messages, isStreaming, finalAssistantId, busy, forkingMessageId, onRegenerate, onFork, onNearEndChange, trailingContent },
  ref,
) {
  const plainRef = useRef<HTMLDivElement>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const virtualized = messages.length >= VIRTUALIZE_THRESHOLD;
  const wasVirtualizedRef = useRef(virtualized);
  const lastPlainScrollTopRef = useRef(0);
  const lastPlainNearEndRef = useRef(true);

  // The plain transcript is intentionally retained for short chats, but the
  // implementation switch at the threshold must not discard a reader's
  // viewport. Track the last real scroll position while the plain node still
  // exists, then restore that pixel offset as soon as Virtuoso mounts. A
  // reader already at the end keeps the existing bottom-biased initialization.
  useLayoutEffect(() => {
    if (!virtualized && plainRef.current) {
      lastPlainScrollTopRef.current = plainRef.current.scrollTop;
    }
  }, [messages.length, virtualized]);

  useLayoutEffect(() => {
    const wasVirtualized = wasVirtualizedRef.current;
    wasVirtualizedRef.current = virtualized;
    if (!virtualized || wasVirtualized || lastPlainNearEndRef.current) return undefined;

    const restoreScroll = () => {
      virtuosoRef.current?.scrollTo({ top: lastPlainScrollTopRef.current, behavior: "auto" });
    };
    // Virtuoso exposes its handle during mount, but a second frame covers
    // browsers that finish creating the internal scroller one layout later.
    restoreScroll();
    if (typeof window.requestAnimationFrame !== "function") return undefined;
    const frame = window.requestAnimationFrame(restoreScroll);
    return () => window.cancelAnimationFrame(frame);
  }, [virtualized]);

  // Virtuoso remounts its Footer subtree whenever the `components.Footer`
  // *function* identity changes -- a fresh arrow function here every render
  // (streaming pushes a render per token) tore down and rebuilt the
  // streaming bubble every frame. `components` itself must still get a new
  // object each render (Virtuoso only redraws the slot when that reference
  // changes), but Footer's own identity stays stable via the ref, so React
  // reconciles the redraw as an update to the existing instance rather than
  // an unmount/remount.
  const trailingRef = useRef(trailingContent);
  trailingRef.current = trailingContent;
  const Footer = useCallback(() => <>{trailingRef.current}</>, []);

  useImperativeHandle(ref, () => ({
    scrollToBottom: () => {
      if (virtualized) {
        // Not scrollToIndex(last message): the in-flight streaming bubble
        // lives in the Footer slot, *below* the final item, so targeting the
        // last item leaves the answer being typed out of view for the whole
        // response. Scroll the virtualized scroller to its true bottom, which
        // is what the plain path's scrollTop = scrollHeight already does.
        virtuosoRef.current?.scrollTo({ top: Number.MAX_SAFE_INTEGER, behavior: "auto" });
      } else if (plainRef.current) {
        plainRef.current.scrollTop = plainRef.current.scrollHeight;
      }
    },
  }), [virtualized]);

  const renderCard = (message: ChatMessage, index: number) => (
    <MessageCard
      key={message.id ?? `${message.role}-${index}`}
      message={message}
      isFinalAssistant={message.id === finalAssistantId}
      busy={busy}
      onRegenerate={() => onRegenerate(message, index)}
      onFork={() => onFork(message)}
      forking={forkingMessageId === message.id}
    />
  );

  if (!virtualized) {
    return (
      <div
        className="transcript"
        ref={plainRef}
        onScroll={() => {
          const node = plainRef.current;
          if (!node) return;
          lastPlainScrollTopRef.current = node.scrollTop;
          lastPlainNearEndRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
          onNearEndChange(lastPlainNearEndRef.current);
        }}
      >
        {messages.map((message, index) => renderCard(message, index))}
        {trailingContent}
      </div>
    );
  }

  return (
    <Virtuoso
      ref={virtuosoRef}
      className="transcript transcript-virtual"
      data={messages}
      computeItemKey={(index, message) => message.id ?? `${message.role}-${index}`}
      followOutput={isStreaming ? "smooth" : false}
      initialTopMostItemIndex={lastPlainNearEndRef.current ? messages.length - 1 : 0}
      alignToBottom
      atBottomStateChange={onNearEndChange}
      itemContent={(index, message) => renderCard(message, index)}
      components={{ Footer }}
    />
  );
});
