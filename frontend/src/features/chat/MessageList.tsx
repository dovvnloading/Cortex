import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { usePrefersReducedMotion } from "../../hooks/useMediaQuery";
import { MessageCard } from "./MessageCard";

const VIRTUALIZE_THRESHOLD = 40;

/**
 * How close to the end (in pixels) still counts as "at the bottom". Both the
 * plain transcript and Virtuoso use it, so a reader is followed -- or not --
 * the same way on either side of the virtualization threshold.
 */
const NEAR_END_PX = 80;

export type MessageListHandle = {
  /**
   * Scroll to the end. `"instant"` (the default) is for following streamed
   * output, which asks on every frame: a smooth scroll restarts its animation
   * each time, so the view lags behind the text and never settles. `"smooth"`
   * is for the reader's own "Jump to latest", and becomes instant when the
   * person has asked for reduced motion.
   */
  scrollToBottom: (behavior?: "instant" | "smooth") => void;
};

type Props = {
  messages: ChatMessage[];
  isStreaming: boolean;
  finalAssistantId: string | null;
  busy: boolean;
  forkingMessageId: string | null;
  /** Keep this and `onFork` referentially stable; a new function each render re-renders every card. */
  onRegenerate: (message: ChatMessage, index: number) => void;
  onFork: (message: ChatMessage) => void;
  /** Stores a memory the model suggested. Keep it stable, like the two above. */
  onSaveMemory?: (memo: string) => Promise<boolean>;
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
  { messages, isStreaming, finalAssistantId, busy, forkingMessageId, onRegenerate, onFork, onSaveMemory, onNearEndChange, trailingContent },
  ref,
) {
  const plainRef = useRef<HTMLDivElement>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const virtualized = messages.length >= VIRTUALIZE_THRESHOLD;
  // Scrolling started from script is not covered by the stylesheet's
  // reduced-motion rule, so it is decided here.
  const reducedMotion = usePrefersReducedMotion();
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
    scrollToBottom: (requested = "instant") => {
      const behavior = reducedMotion ? "instant" : requested;
      if (virtualized) {
        // Not scrollToIndex(last message): the in-flight streaming bubble
        // lives in the Footer slot, *below* the final item, so targeting the
        // last item leaves the answer being typed out of view for the whole
        // response. Scroll the virtualized scroller to its true bottom, which
        // is what the plain path does with the scroll height.
        virtuosoRef.current?.scrollTo({ top: Number.MAX_SAFE_INTEGER, behavior });
      } else if (plainRef.current) {
        const node = plainRef.current;
        // An explicit behavior wins over the stylesheet's `scroll-behavior:
        // smooth` on .transcript; assigning scrollTop would not, and would
        // start a fresh animation on every streamed frame.
        if (typeof node.scrollTo === "function") node.scrollTo({ top: node.scrollHeight, behavior });
        else node.scrollTop = node.scrollHeight;
      }
    },
  }), [virtualized, reducedMotion]);

  const renderCard = (message: ChatMessage, index: number) => (
    <MessageCard
      key={message.id ?? `${message.role}-${index}`}
      message={message}
      index={index}
      isFinalAssistant={message.id === finalAssistantId}
      busy={busy}
      // Passed through as received, not wrapped: a closure per card would be a
      // new prop on every render and defeat MessageCard's memo. Callers keep
      // these stable (useCallback).
      onRegenerate={onRegenerate}
      onFork={onFork}
      onSaveMemory={onSaveMemory}
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
          lastPlainNearEndRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < NEAR_END_PX;
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
      // Virtuoso animates this itself, in script, so the stylesheet's
      // reduced-motion rule does not reach it.
      followOutput={isStreaming ? (reducedMotion ? "auto" : "smooth") : false}
      initialTopMostItemIndex={lastPlainNearEndRef.current ? messages.length - 1 : 0}
      alignToBottom
      atBottomThreshold={NEAR_END_PX}
      atBottomStateChange={onNearEndChange}
      itemContent={(index, message) => renderCard(message, index)}
      components={{ Footer }}
    />
  );
});
