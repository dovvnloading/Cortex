import { useCallback, useRef } from "react";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";

const RECONNECT_DELAY_MS = 250;
const ACTIVE_JOB_KEY = "cortex.active.generation";

export type PersistedJob = { jobId: string; threadId: string; lastEventId: number };

export function readActiveJob(): PersistedJob | null {
  const raw = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as PersistedJob;
  } catch {
    return null;
  }
}

function persistActiveJob(job: PersistedJob): void {
  window.sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job));
}

function clearActiveJob(): void {
  window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

type OnCompleted = (threadId: string) => Promise<void>;
type OnFailed = (threadId: string, message: string) => void;

/**
 * Coalesces many rapid push() calls into at most one flush per animation
 * frame, so a fast token stream doesn't trigger a store update (and every
 * subscriber's re-render) per SSE event. flushNow() guarantees no buffered
 * text is ever silently dropped on an early exit (abort, terminal event).
 */
function createRafBatchedFlusher(flush: (buffered: string) => void) {
  let buffer = "";
  let rafHandle: number | null = null;
  const flushNow = () => {
    if (rafHandle != null) {
      cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
    if (buffer) {
      const pending = buffer;
      buffer = "";
      flush(pending);
    }
  };
  return {
    push(chunk: string) {
      buffer += chunk;
      if (rafHandle == null) {
        rafHandle = requestAnimationFrame(() => {
          rafHandle = null;
          const pending = buffer;
          buffer = "";
          flush(pending);
        });
      }
    },
    flushNow,
  };
}

/**
 * Owns the SSE reconnect loop for a single global generation job and writes
 * tokens/status into useChatStore. sessionStorage remains the durability
 * side-channel that survives a full ChatPage unmount (e.g. a Settings
 * round-trip) or a page reload; the store is the fast in-memory view of it.
 * Error handling stays with the caller (ChatPage) via onFailed, since it's
 * displayed scoped to whichever thread is currently being viewed.
 */
export function useGenerationStream(api: CortexApi) {
  const consumingRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const consume = useCallback(
    async (job: PersistedJob, onCompleted: OnCompleted, onFailed: OnFailed): Promise<void> => {
      if (consumingRef.current === job.jobId) return;
      consumingRef.current = job.jobId;
      const controller = new AbortController();
      abortRef.current = controller;
      let cursor = job.lastEventId;
      let terminal = false;
      useChatStore.getState().setStatusText(job.jobId, "Connecting to generation...");
      const contentFlusher = createRafBatchedFlusher((buffered) => useChatStore.getState().appendContentToken(job.jobId, buffered));
      const thoughtsFlusher = createRafBatchedFlusher((buffered) => useChatStore.getState().appendThinkingToken(job.jobId, buffered));

      try {
        while (!terminal && !controller.signal.aborted) {
          try {
            await api.streamGeneration(
              job.jobId,
              (event) => {
                if (event.event_id <= cursor || event.thread_id !== job.threadId) return;
                cursor = event.event_id;
                persistActiveJob({ ...job, lastEventId: cursor });
                const data = event.data ?? {};
                if (typeof data.message === "string") useChatStore.getState().setStatusText(job.jobId, data.message);
                if (event.event === "generation.cancelling") useChatStore.getState().markStopping(job.jobId);
                if (event.event === "generation.thinking_delta" && typeof data.delta === "string") {
                  thoughtsFlusher.push(data.delta);
                }
                if (event.event === "generation.content_delta" && typeof data.delta === "string") {
                  contentFlusher.push(data.delta);
                }
                if (event.event === "generation.persisting") {
                  // Every token has been sent; only backend bookkeeping
                  // (saving the message, generating a chat title) remains.
                  // Flush immediately so the UI can drop the "still typing"
                  // look right away instead of waiting on that bookkeeping.
                  contentFlusher.flushNow();
                  thoughtsFlusher.flushNow();
                  useChatStore.getState().markContentReady(job.jobId);
                }
                if (event.event === "generation.completed") {
                  terminal = true;
                  void onCompleted(job.threadId);
                }
                if (event.event === "generation.failed" || event.event === "generation.cancelled") {
                  terminal = true;
                  onFailed(job.threadId, typeof data.message === "string" ? data.message : "Generation did not complete.");
                  void onCompleted(job.threadId);
                }
              },
              { signal: controller.signal, afterEventId: cursor },
            );
            if (!terminal && !controller.signal.aborted) {
              useChatStore.getState().setStatusText(job.jobId, "Connection interrupted. Reconnecting...");
              await delay(RECONNECT_DELAY_MS);
            }
          } catch (streamError) {
            if (controller.signal.aborted) return;
            if (streamError instanceof ApiError && streamError.status === 401) return;
            const snapshot = await api.generationStatus(job.jobId);
            if (snapshot.status === "succeeded" || snapshot.status === "failed" || snapshot.status === "cancelled") {
              terminal = true;
              if (snapshot.status !== "succeeded") {
                onFailed(job.threadId, snapshot.error ?? "Generation did not complete.");
              }
              await onCompleted(job.threadId);
            } else {
              if (snapshot.status === "cancelling") useChatStore.getState().markStopping(job.jobId);
              useChatStore.getState().setStatusText(job.jobId, "Connection interrupted. Reconnecting...");
              await delay(RECONNECT_DELAY_MS);
            }
          }
        }
      } catch (streamError) {
        if (!controller.signal.aborted) {
          onFailed(job.threadId, streamError instanceof Error ? streamError.message : "Generation stream failed.");
        }
      } finally {
        // Flush before any terminal reset so a token buffered just before
        // completion/abort is never silently dropped.
        contentFlusher.flushNow();
        thoughtsFlusher.flushNow();
        if (terminal) {
          const stored = readActiveJob();
          if (stored?.jobId === job.jobId) clearActiveJob();
          useChatStore.getState().endGeneration(job.jobId);
        }
        consumingRef.current = null;
      }
    },
    [api],
  );

  const start = useCallback(
    (jobId: string, threadId: string, onCompleted: OnCompleted, onFailed: OnFailed) => {
      const job: PersistedJob = { jobId, threadId, lastEventId: 0 };
      persistActiveJob(job);
      useChatStore.getState().beginGeneration(jobId, threadId);
      void consume(job, onCompleted, onFailed);
    },
    [consume],
  );

  return { start, consume, stop };
}
