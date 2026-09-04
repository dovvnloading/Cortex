import { useCallback, useRef } from "react";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { useUiStore } from "../stores/useUiStore";

export const RECONNECT_BASE_DELAY_MS = 250;
export const RECONNECT_MAX_DELAY_MS = 30_000;
const RECONNECT_JITTER_RATIO = 0.2;
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
  let raw: string | null;
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

export function reconnectDelay(attempt: number, random = Math.random): number {
  const exponential = Math.min(
    RECONNECT_MAX_DELAY_MS,
    RECONNECT_BASE_DELAY_MS * (2 ** Math.max(0, attempt)),
  );
  const jitter = 1 + ((random() * 2) - 1) * RECONNECT_JITTER_RATIO;
  return Math.min(RECONNECT_MAX_DELAY_MS, Math.max(0, Math.round(exponential * jitter)));
}

function delay(milliseconds: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    let timer: number | null = null;
    let settled = false;
    const finish = (completed: boolean) => {
      if (settled) return;
      settled = true;
      if (timer != null) window.clearTimeout(timer);
      timer = null;
      signal.removeEventListener("abort", onAbort);
      resolve(completed);
    };
    const onAbort = () => {
      finish(false);
    };
    if (signal.aborted) {
      finish(false);
      return;
    }
    timer = window.setTimeout(() => finish(true), milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
}

function waitForReconnect(jobId: string, attempt: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false);
  if (!window.navigator.onLine) {
    useChatStore.getState().setStatusText(jobId, "Connection paused while offline. Waiting for network...");
    return new Promise((resolve) => {
      let settled = false;
      const finish = (completed: boolean) => {
        if (settled) return;
        settled = true;
        window.removeEventListener("online", onOnline);
        signal.removeEventListener("abort", onAbort);
        resolve(completed);
      };
      const onOnline = () => {
        useChatStore.getState().setStatusText(jobId, "Connection restored. Reconnecting...");
        finish(true);
      };
      const onAbort = () => {
        finish(false);
      };
      window.addEventListener("online", onOnline);
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
      else if (window.navigator.onLine) onOnline();
    });
  }
  const milliseconds = reconnectDelay(attempt);
  useChatStore.getState().setStatusText(
    jobId,
    `Connection interrupted. Retrying in ${Math.ceil(milliseconds / 1000)}s...`,
  );
  return delay(milliseconds, signal);
}

type OnCompleted = (threadId: string, clearRequested?: boolean, jobId?: string) => Promise<void>;
type OnFailed = (threadId: string, message: string) => void;
type OnSessionExpired = () => void;

function hasClearRequest(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const result = value as { clear_requested?: unknown; memory_command?: unknown };
  if (result.clear_requested === true) return true;
  if (!result.memory_command || typeof result.memory_command !== "object") return false;
  return (result.memory_command as { clear_requested?: unknown }).clear_requested === true;
}

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
      let reconnectAttempt = 0;
      let terminal = false;
      let sessionExpired = false;
      let rejectionNotified = false;
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
                // Any event at all means this connection attempt succeeded;
                // without this a generation that reconnects a few times
                // over a long run keeps compounding the same backoff delay
                // even though every intervening connection worked fine.
                reconnectAttempt = 0;
                if (event.event_id <= cursor || event.thread_id !== job.threadId) return;
                cursor = event.event_id;
                const isTokenDelta = event.event === "generation.content_delta"
                  || event.event === "generation.thinking_delta";
                // Storage is the cold-start side channel; the store holds the
                // live cursor. A cold start deliberately replays the job from
                // event 0 (see ChatPage's resume effect), so the persisted
                // cursor is never what a resume reads back -- only the job and
                // thread identity are. Skipping the write on token deltas
                // therefore costs nothing and takes a JSON.stringify plus a
                // synchronous setItem out of the hot path, where they were
                // running once per 80 characters of answer, right next to the
                // rAF batching that exists to keep that path cheap.
                if (!isTokenDelta) persistActiveJob({ ...job, lastEventId: cursor });
                useChatStore.getState().setGenerationCursor(job.jobId, cursor);
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
                // The assistant asked to run something locally and the harness
                // refused. The rejected block is stripped from the answer, so
                // without this the task would simply never happen and the user
                // would be left guessing why. The same payload rides both the
                // status event and the job result, so it is announced once.
                const rejection = data.code_execution_rejection;
                if (
                  !rejectionNotified
                  && rejection
                  && typeof rejection === "object"
                  && typeof (rejection as { message?: unknown }).message === "string"
                ) {
                  rejectionNotified = true;
                  useUiStore.getState().notify((rejection as { message: string }).message, "info");
                }
                if (event.event === "generation.completed") {
                  terminal = true;
                  const clearRequested = hasClearRequest(data);
                  completion = clearRequested
                    ? onCompleted(job.threadId, true, job.jobId)
                    : onCompleted(job.threadId);
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
              if (!(await waitForReconnect(job.jobId, reconnectAttempt, controller.signal))) return;
              reconnectAttempt += 1;
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
                const clearRequested = snapshot.status === "succeeded" && hasClearRequest(snapshot.result);
                completion = clearRequested
                  ? onCompleted(job.threadId, true, job.jobId)
                  : onCompleted(job.threadId);
              } else {
                if (snapshot.status === "cancelling") useChatStore.getState().markStopping(job.jobId);
                if (!(await waitForReconnect(job.jobId, reconnectAttempt, controller.signal))) return;
                reconnectAttempt += 1;
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
              if (!(await waitForReconnect(job.jobId, reconnectAttempt, controller.signal))) return;
              reconnectAttempt += 1;
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
        // Only release the claim if it is still ours: a consumer that is
        // finishing late (its terminal reload is awaited above) must not
        // clear the marker a newer job has since installed, which would let
        // a second consumer attach to that newer job in parallel.
        if (consumingRef.current === job.jobId) consumingRef.current = null;
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
