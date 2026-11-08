import { useCallback, useEffect, useRef } from "react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { readActiveJob, reconnectDelay, useGenerationStream, type PersistedJob } from "../../hooks/useGenerationStream";
import { useChatStore } from "../../stores/useChatStore";

type Props = {
  api: CortexApi;
  onSessionExpired: () => void;
};

/**
 * Consumes the running generation's event stream for as long as the workspace
 * is up, whichever route is showing.
 *
 * The consumer used to belong to ChatPage, so opening Settings mid-answer
 * aborted the stream. The backend only keeps a bounded tail of a job's events,
 * so coming back after a while resumed the bubble partway through the answer,
 * and a job that finished meanwhile was not reconciled until the chat page
 * returned. Living above the route switch removes both: the store keeps
 * receiving text, and the chat is reloaded and the sidebar updated the moment
 * the job ends.
 *
 * It renders nothing. A page starts a job with `trackGeneration` and reads the
 * text from the store; how the job ended arrives as `lastCompletion` /
 * `lastFailure` for the chat view to react to.
 */
export function GenerationStreamHost({ api, onSessionExpired }: Props): null {
  const { consume, stop } = useGenerationStream(api, onSessionExpired);
  // Only the first look may adopt a job left in session storage. Adopting one
  // whenever the store goes idle would resurrect a finished job if clearing
  // the stored copy had failed.
  const coldStartCheckedRef = useRef(false);
  // The job this host has already attached to. It is only released after the
  // consumer exits (including authentication refusal), so no two readers
  // follow the same generation concurrently. An expired session that another
  // request already renewed should not leave the composer stuck until reload.
  const attachedJobIdRef = useRef<string | null>(null);

  const onCompleted = useCallback(
    async (threadId: string, clearRequested = false, jobId?: string): Promise<void> => {
      // Reload here, before the consumer ends the generation, so the pending
      // bubble is not dropped a moment before the saved message can replace it.
      let chat: ChatResponse | null = null;
      try {
        chat = await api.chat(threadId);
        useChatStore.getState().upsertChatSummary(chat);
      } catch {
        // The answer is saved; only the reload failed. The chat view reports it.
      }
      useChatStore.getState().recordCompletion({ jobId: jobId ?? null, threadId, chat, clearRequested });
    },
    [api],
  );

  const onFailed = useCallback((threadId: string, message: string): void => {
    useChatStore.getState().recordFailure({ threadId, message });
  }, []);

  useEffect(() => stop, [stop]);

  // Attach to whatever job the store tracks: one kept across a workspace
  // remount, or -- once, on a cold start -- one left in session storage by a
  // reload, both looked for a tick after mounting; and any job a page starts
  // later, the moment the store moves to it, so the stream opens exactly as
  // early as it did when the page owned it.
  //
  // The mount-time look is deferred so React StrictMode's simulated unmount
  // cancels the first attempt instead of racing a second consumer against it.
  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | null = null;
    let authAttempt = 0;
    let retryJobId: string | null = null;
    const follow = (job: PersistedJob): void => {
      if (disposed || attachedJobIdRef.current === job.jobId) return;
      if (retryJobId !== job.jobId) {
        retryJobId = job.jobId;
        authAttempt = 0;
      }
      attachedJobIdRef.current = job.jobId;
      void consume(job, onCompleted, onFailed).then((outcome) => {
        if (disposed || outcome !== "auth" || attachedJobIdRef.current !== job.jobId) return;
        // The consumer is now *fully* stopped, including its status fallback,
        // so dropping this guard cannot create concurrent SSE readers.
        attachedJobIdRef.current = null;
        if (useChatStore.getState().generation.jobId !== job.jobId) return;
        // A working replacement session may already exist, so the parent's
        // onSessionExpired deliberately does nothing. Keep the job and cursor,
        // and retry with bounded backoff instead of stranding the composer.
        const delayMs = reconnectDelay(authAttempt);
        authAttempt = Math.min(authAttempt + 1, 20);
        if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null;
          followTrackedJob();
        }, delayMs);
      });
    };
    const followTrackedJob = (): void => {
      if (disposed) return;
      const { generation, generationCursor } = useChatStore.getState();
      if (!generation.jobId || !generation.threadId) return;
      follow({ jobId: generation.jobId, threadId: generation.threadId, lastEventId: generationCursor });
    };

    const timer = window.setTimeout(() => {
      if (disposed) return;
      const { generation, beginGeneration } = useChatStore.getState();
      if (generation.jobId) {
        followTrackedJob();
      } else if (!coldStartCheckedRef.current) {
        const stored = readActiveJob();
        if (stored) {
          // A cold start replays from event zero; the store has no text yet.
          // Claim before beginGeneration triggers the store subscriber.
          const job: PersistedJob = { ...stored, lastEventId: 0 };
          attachedJobIdRef.current = job.jobId;
          beginGeneration(job.jobId, job.threadId);
          void consume(job, onCompleted, onFailed).then((outcome) => {
            if (disposed || outcome !== "auth" || attachedJobIdRef.current !== job.jobId) return;
            attachedJobIdRef.current = null;
            const delayMs = reconnectDelay(authAttempt);
            authAttempt = Math.min(authAttempt + 1, 20);
            reconnectTimer = window.setTimeout(() => {
              reconnectTimer = null;
              followTrackedJob();
            }, delayMs);
          });
        }
      }
      coldStartCheckedRef.current = true;
    }, 0);
    const unsubscribe = useChatStore.subscribe((state, previous) => {
      if (state.generation.jobId && state.generation.jobId !== previous.generation.jobId) followTrackedJob();
    });
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      unsubscribe();
    };
  }, [consume, onCompleted, onFailed]);

  return null;
}
