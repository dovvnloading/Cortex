import { useEffect, useRef, useState } from "react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import { useChatStore } from "../../stores/useChatStore";

/**
 * What to tell someone who cannot see the answer arrive, once it is saved:
 * whether it finished or was stopped, and how long it was when that is known.
 */
function describeFinishedResponse(chat: ChatResponse | null): string {
  const lastAssistant = [...(chat?.messages ?? [])].reverse().find((message) => message.role === "assistant");
  const stats = lastAssistant?.stats;
  if (stats?.stopped) return "Response stopped";
  const tokens = stats?.eval_count;
  if (typeof tokens === "number" && tokens > 0) {
    return `Response complete, ${tokens.toLocaleString()} ${tokens === 1 ? "token" : "tokens"}`;
  }
  return "Response complete";
}

/**
 * A visually hidden, polite live region that says when a response starts,
 * finishes, stops, or fails -- and nothing in between.
 *
 * The transcript itself is deliberately not a live region: a streaming answer
 * changes many times a second, and a screen reader would read it back token by
 * token. The composer's status line reads "Generating..." and then reverts, and
 * the pending bubble is gone as soon as the first token arrives, so without
 * this a person using a screen reader is never told a response began or ended.
 *
 * It follows the chat store rather than props: a stop or a failure ends up
 * published there by the stream host, whichever page was showing. Only changes
 * seen while this is mounted are announced, so an outcome that was already
 * waiting when the page opened is not read out again. A failure is announced
 * briefly; its reason is in the composer's alert, which is read on its own.
 */
export function ResponseAnnouncer({ threadId }: { threadId: string | null }) {
  const [announcement, setAnnouncement] = useState("");
  const threadIdRef = useRef(threadId);
  // The job whose outcome has been announced. A failed or stopped job publishes
  // its failure and then, once the chat is reloaded, a completion as well; only
  // the first says what happened, and the second must not overwrite it.
  const settledJobRef = useRef<string | null>(null);
  // Assigned in an effect rather than during render, as react-hooks/refs requires.
  useEffect(() => {
    threadIdRef.current = threadId;
  });

  useEffect(() => useChatStore.subscribe((state, previous) => {
    const { jobId, phase } = state.generation;
    if (jobId && jobId !== previous.generation.jobId) {
      setAnnouncement("Response started");
    }
    // A stop the person asked for is "stopping" until the job ends, whether it
    // ends as a kept answer (a completion) or as nothing to keep (a failure).
    const userStopped = phase === "stopping";
    const outcome = (announce: string) => {
      if (jobId && settledJobRef.current === jobId) return;
      settledJobRef.current = jobId;
      setAnnouncement(announce);
    };
    const { lastCompletion, lastFailure } = state;
    if (lastFailure && lastFailure !== previous.lastFailure && lastFailure.threadId === threadIdRef.current) {
      outcome(userStopped ? "Response stopped" : "Response failed");
    }
    if (lastCompletion && lastCompletion !== previous.lastCompletion && lastCompletion.threadId === threadIdRef.current) {
      outcome(userStopped ? "Response stopped" : describeFinishedResponse(lastCompletion.chat));
    }
  }), []);

  return <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>;
}
