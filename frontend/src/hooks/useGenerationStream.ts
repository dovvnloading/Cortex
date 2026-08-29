import { useCallback, useRef } from "react";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";

const RECONNECT_DELAY_MS = 250;
const ACTIVE_JOB_KEY = "cortex.active.generation";

export type PersistedJob = { jobId: string; threadId: string; lastEventId: number };

function isPersistedJob(value: unknown): value is PersistedJob {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<PersistedJob>;
  return typeof candidate.jobId === "string"
    && candidate.jobId.length > 0
    && typeof candidate.threadId === "string"
    && candidate.threadId.length > 0
    && typeof candidate.lastEventId === "number"
    && Number.isSafeInteger(candidate.lastEventId)
    && candidate.lastEventId >= 0;
}

/**
 * Session storage is a resilience side-channel here, never the source of
 * truth -- the store is. A browser that denies storage access, or one that
 * has hit its quota, throws on plain getItem/setItem/removeItem. Since
 * persistActiveJob() runs inside the synchronous SSE event handler, an
 * escaping throw would surface to consume() as a *stream* failure: it would
 * reconnect from the same cursor, replay the same event, and throw again,
 * looping forever without ever rendering a token. Treat storage as
 * best-effort instead, exactly as the composer draft helpers already do.
 */
export function readActiveJob(): PersistedJob | null {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return isPersistedJob(value) ? value : null;
  } catch {
    return null;
  }
}

function persistActiveJob(job: PersistedJob): void {
  try {
    window.sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job));
  } catch {
    // The live stream continues from its in-memory cursor; only resume
    // across a full remount is lost.
  }
}

function clearActiveJob(): void {
  try {
    window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
  } catch {
    // Nothing to clean up if storage is unavailable in the first place.
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

type OnCompleted = (threadId: string) => Promise<void>;
type OnFailed = (threadId: string, message: string) => void;
type OnSessionExpired = () => void;

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
export function useGenerationStream(api: CortexApi, onSessionExpired: OnSessionExpired) {
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
      let sessionExpired = false;
      // Set alongside `terminal`, never awaited until the finally block below
      // -- endGeneration() must not clear the store's jobId (and unmount the
      // pending bubble) before the reload it triggers has actually put the
      // real, persisted message in its place. Awaiting inline here isn't an
      // option: the SSE event handler that sets this is a synchronous
      // callback.
      let completion: Promise<void> | null = null;
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
                  completion = onCompleted(job.threadId);
                }
                if (event.event === "generation.failed" || event.event === "generation.cancelled") {
                  terminal = true;
                  onFailed(job.threadId, typeof data.message === "string" ? data.message : "Generation did not complete.");
                  completion = onCompleted(job.threadId);
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
            if (streamError instanceof ApiError && streamError.status === 401) {
              sessionExpired = true;
              break;
            }
            try {
              const snapshot = await api.generationStatus(job.jobId);
              if (snapshot.status === "succeeded" || snapshot.status === "failed" || snapshot.status === "cancelled") {
                terminal = true;
                if (snapshot.status !== "succeeded") {
                  onFailed(job.threadId, snapshot.error ?? "Generation did not complete.");
                }
                completion = onCompleted(job.threadId);
              } else {
                if (snapshot.status === "cancelling") useChatStore.getState().markStopping(job.jobId);
                useChatStore.getState().setStatusText(job.jobId, "Connection interrupted. Reconnecting...");
                await delay(RECONNECT_DELAY_MS);
              }
            } catch (statusError) {
              // Both the live stream AND the status-check fallback just
              // failed. Letting this escape uncaught would jump straight to
              // the outer catch/finally below without ever setting
              // `terminal` -- onFailed() would still fire, but nothing
              // would ever call endGeneration(), leaving the composer and
              // the pending message bubble reporting "Generating" forever
              // with no live connection left to correct it. Treat it like
              // any other dropped connection instead: keep retrying.
              if (statusError instanceof ApiError && statusError.status === 401) {
                sessionExpired = true;
                break;
              }
              if (statusError instanceof ApiError && (statusError.status === 403 || statusError.status === 404)) {
                terminal = true;
                onFailed(job.threadId, statusError.detail || "Generation is no longer available.");
                break;
              }
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
        if (terminal && completion) {
          // Wait for the reload that puts the real, persisted message in
          // the transcript before dropping the store's jobId below --
          // otherwise the pending bubble unmounts (jobId cleared) a beat
          // before the real one is ready to take its place, and the
          // response visibly vanishes for the length of that request
          // before "popping" back in once it resolves. onCompleted
          // implementations (reconcileChat) already catch and report
          // their own failures, so this is just a wait, not error
          // handling.
          await completion.catch(() => undefined);
        }
        if (terminal || sessionExpired) {
          const stored = readActiveJob();
          if (stored?.jobId === job.jobId) clearActiveJob();
          useChatStore.getState().endGeneration(job.jobId);
        }
        consumingRef.current = null;
        if (sessionExpired) onSessionExpired();
      }
    },
    [api, onSessionExpired],
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
