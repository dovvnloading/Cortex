import { useCallback, useEffect, useMemo, useRef } from "react";
import type { JobAccepted, JobStatusResponse, ModelResponse } from "../../../contracts/cortex-api";
import { ApiError, describeApiError, type CortexApi } from "../api/client";
import { describeModelProgress } from "../lib/modelJobs";
import { readStoredModelJob, useModelStore, type ActiveModelJob } from "../stores/useModelStore";
import type { ToastKind } from "../stores/useUiStore";

/** How a model job ended, for the caller that started it. */
export type ModelJobResult = {
  succeeded: boolean;
  /** What the finished job returned; `null` unless it succeeded. */
  data: Record<string, unknown> | null;
  /** Why it failed, in words for the person; `null` when it did not fail. */
  failure: string | null;
  /** The person asked for it to stop, and it did. */
  cancelled: boolean;
  /**
   * This screen stopped following the job (it was closed) before the job
   * ended. The job itself carries on; the next screen picks it up again.
   */
  detached: boolean;
};

export type ModelJobOptions = {
  /** Report an unreachable Ollama as a problem with this job. Default true. */
  checkOllamaConnection?: boolean;
  /** Announce success with the generic toast. Default true. */
  notifyOnSuccess?: boolean;
  /** Original model selection for a GGUF job, retained across workspace reloads. */
  selectedChatModelAtStart?: string | null;
  /** GGUF folder configured at download admission, retained through reloads. */
  ggufDirectoryAtStart?: string | null;
};

type Dependencies = {
  api: CortexApi;
  notify: (message: string, kind?: ToastKind) => void;
  /** The inventory a finished job refreshed. */
  onModels: (models: ModelResponse) => void;
  /** A download that finished while nothing was waiting on it, which is one picked up again after a reload. */
  onDownloaded: (
    filename: string,
    selectedChatModelAtStart: string | null | undefined,
    ggufDirectoryAtStart: string | null | undefined,
  ) => Promise<void>;
};

type Tracking = { jobId: string; controller: AbortController; cancelRequested: boolean };

const DETACHED: ModelJobResult = { succeeded: false, data: null, failure: null, cancelled: false, detached: true };
// A dropped SSE connection must not hide a live download's progress or Cancel.
const RECONNECT_INITIAL_DELAY_MS = 250;
const RECONNECT_MAX_DELAY_MS = 5000;

function waitForReconnect(signal: AbortSignal, delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const done = () => {
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timeout = window.setTimeout(done, delayMs);
    signal.addEventListener("abort", done, { once: true });
  });
}

/**
 * The backend has no job this session can follow: it finished long ago, or the
 * backend restarted (404), or it belongs to another session (403). Either way
 * it can never be followed, so a remembered one is dropped rather than reported.
 */
function isJobGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

/**
 * Follow the backend's model jobs (an Ollama check or pull, a GGUF download).
 *
 * A job lives on the backend and outlives this screen, so following one is
 * separate from owning it: the job being followed is kept in the model store
 * and mirrored to session storage, the stream is cut when this screen goes
 * away, and the next one reads the job back and continues from the last event
 * it showed. Only one job is followed at a time -- the one the progress row
 * shows and Cancel stops. A job started while another is followed (a rescan
 * while a download runs) is still watched to its end and reported, but leaves
 * the progress row alone.
 */
export function useModelJobs(dependencies: Dependencies): {
  /** Follow a job the backend just accepted, to its end. Never rejects. */
  run: (accepted: JobAccepted, label?: string, options?: ModelJobOptions) => Promise<ModelJobResult>;
  /** Ask the backend to stop the job being followed. */
  cancel: () => Promise<void>;
} {
  const latest = useRef(dependencies);
  useEffect(() => {
    latest.current = dependencies;
  });
  const alive = useRef(false);
  const tracked = useRef<Tracking | null>(null);
  const untracked = useRef(new Set<AbortController>());

  const follow = useCallback(async (
    job: ActiveModelJob,
    options: Required<Pick<ModelJobOptions, "checkOllamaConnection" | "notifyOnSuccess">>,
  ): Promise<ModelJobResult> => {
    // Nothing to report to once the screen is gone; the job carries on and
    // the next screen picks it up from what the store remembers.
    const store = useModelStore.getState();
    if (!alive.current) {
      if (store.activeJob === null) store.setActiveJob(job);
      return DETACHED;
    }
    const { api, notify, onModels } = latest.current;
    const controller = new AbortController();
    const tracking: Tracking | null = tracked.current === null
      ? { jobId: job.jobId, controller, cancelRequested: false }
      : null;
    if (tracking) {
      tracked.current = tracking;
      store.setActiveJob(job);
      store.setModelBusy(true);
      store.setModelProgress({
        model: job.model,
        status: job.lastEventId > 0 ? "Reconnecting..." : "Starting...",
        percent: null,
      });
    } else {
      untracked.current.add(controller);
    }
    // While attached this screen may show what happens; once its stream is cut
    // (the screen closed, or a newer one took over) it must not.
    const attached = () => alive.current && !controller.signal.aborted;
    const cancelled = () => tracking?.cancelRequested === true;
    // Whether the job is finished as far as this screen can tell, so the
    // reminder of it can be dropped. A job that may still be running stays.
    let settled = false;
    let completedData: Record<string, unknown> | null = null;
    let failureMessage: string | null = null;
    try {
      let terminalStatus: JobStatusResponse["status"] | null = null;
      let lastEventId = job.lastEventId;
      let reconnectCount = 0;
      // Keep following the same job, with the same Cancel controller, across
      // transient network/auth loss and an SSE close while the job still runs.
      // A retry replaces only the stream, never the remembered job identifier.
      while (attached()) {
        try {
          const terminalEvent = await api.streamJob(job.jobId, (event) => {
            lastEventId = Math.max(lastEventId, event.id);
            if (tracking && attached()) {
              useModelStore.getState().advanceActiveJob(job.jobId, event.id);
              const progress = describeModelProgress(event, job.model);
              if (progress) {
                useModelStore.getState().setModelProgress(
                  cancelled() ? { ...progress, status: "Cancelling...", cancelling: true } : progress,
                );
              }
            }
            if (event.kind === "completed") completedData = event.data ?? null;
            if (event.kind === "error") {
              const message = event.data?.message;
              failureMessage = typeof message === "string" && message ? message : "Model operation failed.";
            }
          }, { signal: controller.signal, afterEventId: lastEventId > 0 ? lastEventId : undefined });
          if (!attached()) return DETACHED;

          // A clean SSE close without a terminal event is not a completed
          // operation. Reconcile against the durable backend snapshot, and
          // reconnect if the worker is still active.
          terminalStatus = terminalEvent ? terminalEvent.status : null;
          if (!terminalEvent) {
            const snapshot = await api.jobStatus(job.jobId);
            if (!attached()) return DETACHED;
            terminalStatus = snapshot.status;
            if (snapshot.status === "succeeded") completedData = snapshot.result ?? null;
            if (snapshot.status === "failed" || snapshot.status === "cancelled") {
              failureMessage = snapshot.error ?? null;
            }
          }
          if (terminalStatus === "succeeded" || terminalStatus === "failed" || terminalStatus === "cancelled") {
            break;
          }
        } catch (error) {
          if (!attached()) return DETACHED;
          // A missing/foreign job cannot be retried; ordinary failures are
          // handled by the existing outer catch. A lost transport says
          // nothing about whether the backend worker is still running.
          if (isJobGone(error) || !(error instanceof ApiError && (error.kind === "network" || error.kind === "auth"))) {
            throw error;
          }
        }

        if (tracking) {
          const current = useModelStore.getState().modelProgress;
          useModelStore.getState().setModelProgress({
            model: job.model,
            percent: current?.percent ?? null,
            status: cancelled() ? "Cancelling..." : "Reconnecting...",
            ...(cancelled() ? { cancelling: true } : {}),
          });
        }
        // Bound the *delay*, not the number of attempts: an offline backend
        // must not permanently orphan a multi-GB job in the still-open UI.
        const delay = Math.min(RECONNECT_INITIAL_DELAY_MS * 2 ** Math.min(reconnectCount, 5), RECONNECT_MAX_DELAY_MS);
        reconnectCount += 1;
        await waitForReconnect(controller.signal, delay);
      }
      if (!attached()) return DETACHED;

      if (terminalStatus === "failed" || terminalStatus === "cancelled") {
        settled = true;
        const byRequest = terminalStatus === "cancelled" && cancelled();
        if (byRequest) {
          if (tracking) useModelStore.getState().setModelProgress({ model: job.model, status: "Cancelled.", percent: null });
          notify(job.kind === "gguf_download" ? "Download cancelled." : "Model operation cancelled.", "info");
          return { succeeded: false, data: null, failure: null, cancelled: true, detached: false };
        }
        const failure = failureMessage
          ?? (terminalStatus === "cancelled" ? "Model operation was cancelled." : "Model operation failed.");
        notify(failure, "error");
        return { succeeded: false, data: null, failure, cancelled: false, detached: false };
      }
      const refreshedModels = await api.models();
      if (!attached()) return DETACHED;
      settled = true;
      onModels(refreshedModels);
      if (options.checkOllamaConnection && !refreshedModels.connection?.success) {
        notify(refreshedModels.connection?.message ?? "Cortex could not reach Ollama.", "error");
      } else if (options.notifyOnSuccess) {
        notify(job.model === "local model inventory" ? "Local model inventory refreshed." : "Model operation completed.", "success");
      }
      return { succeeded: true, data: completedData, failure: null, cancelled: false, detached: false };
    } catch (error) {
      if (!attached()) return DETACHED;
      // No answer says nothing about the job, so a lost connection or an
      // expired session leaves it remembered; a job the backend has forgotten,
      // or any other refusal, does not.
      const unreachable = error instanceof ApiError && (error.kind === "network" || error.kind === "auth");
      settled = !unreachable;
      if (isJobGone(error)) {
        // Most likely it finished while nothing was watching. The folder may
        // have changed, so look again rather than leave a stale list.
        void api.models().then((models) => { if (alive.current) latest.current.onModels(models); }).catch(() => undefined);
        return { succeeded: false, data: null, failure: null, cancelled: false, detached: false };
      }
      const failure = describeApiError(error, "Model operation failed.");
      notify(failure, "error");
      return { succeeded: false, data: null, failure, cancelled: false, detached: false };
    } finally {
      if (tracking) {
        if (tracked.current === tracking) tracked.current = null;
        if (attached()) {
          const current = useModelStore.getState();
          current.setModelBusy(false);
          if (settled) current.setActiveJob(null);
        }
      } else {
        untracked.current.delete(controller);
      }
    }
  }, []);

  const run = useCallback((accepted: JobAccepted, label = "local model inventory", options: ModelJobOptions = {}) => follow(
    {
      jobId: accepted.job_id,
      kind: accepted.kind === "gguf_download" ? "gguf_download" : "models",
      model: label,
      lastEventId: 0,
      ...(options.selectedChatModelAtStart !== undefined
        ? { selectedChatModelAtStart: options.selectedChatModelAtStart }
        : {}),
      ...(options.ggufDirectoryAtStart !== undefined
        ? { ggufDirectoryAtStart: options.ggufDirectoryAtStart }
        : {}),
    },
    { checkOllamaConnection: options.checkOllamaConnection ?? true, notifyOnSuccess: options.notifyOnSuccess ?? true },
  ), [follow]);

  /** Pick a job back up after a reload or a remount, and finish what its caller would have. */
  const attach = useCallback(async (job: ActiveModelJob) => {
    const download = job.kind === "gguf_download";
    const result = await follow(job, { checkOllamaConnection: !download, notifyOnSuccess: !download });
    if (!download || !result.succeeded) return;
    const filename = typeof result.data?.filename === "string" ? result.data.filename : null;
    if (filename) await latest.current.onDownloaded(
      filename, job.selectedChatModelAtStart, job.ggufDirectoryAtStart,
    );
  }, [follow]);

  const cancel = useCallback(async () => {
    const tracking = tracked.current;
    if (!tracking || tracking.cancelRequested) return;
    tracking.cancelRequested = true;
    const store = useModelStore.getState();
    const before = store.modelProgress;
    if (before) store.setModelProgress({ ...before, status: "Cancelling...", cancelling: true });
    try {
      await latest.current.api.cancelJob(tracking.jobId);
    } catch (error) {
      tracking.cancelRequested = false;
      // Put the row back as it was unless a newer update has already replaced it.
      if (useModelStore.getState().modelProgress?.cancelling) store.setModelProgress(before);
      latest.current.notify(describeApiError(error, "Could not cancel the model operation."), "error");
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    const remembered = useModelStore.getState().activeJob ?? readStoredModelJob();
    if (remembered && tracked.current === null) {
      void attach(remembered).catch(() => {
        // Picking the job up again is best effort; the job is unaffected.
      });
    }
    const followers = untracked.current;
    return () => {
      alive.current = false;
      tracked.current?.controller.abort();
      tracked.current = null;
      followers.forEach((controller) => controller.abort());
      followers.clear();
      // The job itself is durable on the backend and stays remembered, but
      // this screen's ownership of it ends here. Do not strand a busy flag in
      // the process-wide store after logout or a remount.
      const store = useModelStore.getState();
      store.setModelBusy(false);
      store.setModelProgress(null);
    };
  }, [attach]);

  return useMemo(() => ({ run, cancel }), [run, cancel]);
}
