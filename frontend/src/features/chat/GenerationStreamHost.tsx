import { useCallback, useEffect, useRef } from "react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { readActiveJob, useGenerationStream, type PersistedJob } from "../../hooks/useGenerationStream";
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
  const trackedJobId = useChatStore((state) => state.generation.jobId);
  // Only the first look may adopt a job left in session storage. Adopting one
  // whenever the store goes idle would resurrect a finished job if clearing
  // the stored copy had failed.
  const coldStartCheckedRef = useRef(false);
  // The job this host has already attached to. Adopting a stored job moves the
  // store, which re-runs the effect below; without this a consumer that
  // stopped on a refused session in the meantime would be attached a second
  // time, behind the very refusal the app is about to act on.
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

  // Attach to whatever job the store tracks: one a page just started, one kept
  // across a workspace remount, or -- once, on a cold start -- one left in
  // session storage by a reload. Deferred a tick so React StrictMode's
  // simulated unmount cancels the first attempt instead of racing a second
  // consumer against it; consume() ignores a job it is already following.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const { generation, generationCursor, beginGeneration } = useChatStore.getState();
      let job: PersistedJob | null = null;
      if (generation.jobId && generation.threadId) {
        job = { jobId: generation.jobId, threadId: generation.threadId, lastEventId: generationCursor };
      } else if (!coldStartCheckedRef.current) {
        // A cold start replays the job from its first event: the store holds
        // no text yet, and a stored cursor would leave the start of the
        // answer missing.
        const stored = readActiveJob();
        if (stored) {
          job = { ...stored, lastEventId: 0 };
          beginGeneration(job.jobId, job.threadId);
        }
      }
      coldStartCheckedRef.current = true;
      if (job && attachedJobIdRef.current !== job.jobId) {
        attachedJobIdRef.current = job.jobId;
        void consume(job, onCompleted, onFailed);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [trackedJobId, consume, onCompleted, onFailed]);

  return null;
}
