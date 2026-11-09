import { useCallback, useEffect, useMemo, useRef } from "react";
import type { JobAccepted, JobStatusResponse, ModelResponse } from "../../../contracts/cortex-api";
import { ApiError, describeApiError, type CortexApi } from "../api/client";
import { describeModelProgress } from "../lib/modelJobs";
import { readStoredModelJob, readStoredSecondaryModelJobs, useModelStore, type ActiveModelJob, type ModelProgress } from "../stores/useModelStore";
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
  cancel: (jobId?: string) => Promise<void>;
} {
  const latest = useRef(dependencies);
  useEffect(() => {
    latest.current = dependencies;
  });
  const alive = useRef(false);
  const tracked = useRef<Tracking | null>(null);
  const untracked = useRef(new Map<string, Tracking>());
  // A backend id may be returned by multiple idempotent admission replays.
  // Join the existing follower instead of creating duplicate streams/Cancel rows.
  const inFlightFollowers = useRef(new Map<string, Promise<ModelJobResult>>());
  // Successful jobs can refresh the shared inventory concurrently. Order by
  // request start, not arrival of responses, so an old snapshot cannot win.
  const inventoryRequestGeneration = useRef(0);

  const follow = useCallback(async (
    job: ActiveModelJob,
    options: Required<Pick<ModelJobOptions, "checkOllamaConnection" | "notifyOnSuccess">>,
  ): Promise<ModelJobResult> => {
    // Nothing to report to once the screen is gone; the job carries on and
    // the next screen picks it up from what the store remembers.
    const store = useModelStore.getState();
    if (!alive.current) {
      if (store.activeJob === null) store.setActiveJob(job);
      else if (store.activeJob.jobId !== job.jobId) store.rememberSecondaryJob(job);
      return DETACHED;
    }
    const { api, notify, onModels } = latest.current;
    const controller = new AbortController();
    const tracking: Tracking | null = tracked.current === null
      ? { jobId: job.jobId, controller, cancelRequested: false }
      : null;
    const secondaryTracking: Tracking | null = tracking === null
      ? { jobId: job.jobId, controller, cancelRequested: false }
      : null;
    const initialProgress: ModelProgress = {
      model: job.model,
      status: job.lastEventId > 0 ? "Reconnecting..." : "Starting...",
      percent: null,
    };
    if (tracking) {
      tracked.current = tracking;
      // A secondary stored before a remount may become the only primary job.
      store.forgetSecondaryJob(job.jobId);
      store.setActiveJob(job);
      store.setModelProgress(initialProgress);
    } else if (secondaryTracking) {
      untracked.current.set(job.jobId, secondaryTracking);
      store.rememberSecondaryJob(job);
      store.setSecondaryProgress(job.jobId, initialProgress);
    }
    store.setModelBusy(true);
    // While attached this screen may show what happens; once its stream is cut
    // (the screen closed, or a newer one took over) it must not.
    const attached = () => alive.current && !controller.signal.aborted;
    const cancelled = () => tracking?.cancelRequested === true || secondaryTracking?.cancelRequested === true;
    const recordProgress = (progress: ModelProgress): void => {
      const current = useModelStore.getState();
      if (tracking) current.setModelProgress(progress);
      else if (secondaryTracking) current.setSecondaryProgress(job.jobId, progress);
    };
    const currentProgress = (): ModelProgress | null => {
      const current = useModelStore.getState();
      return tracking ? current.modelProgress : current.secondaryProgress[job.jobId] ?? null;
    };
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
            if (attached()) {
              if (tracking) useModelStore.getState().advanceActiveJob(job.jobId, event.id);
              else useModelStore.getState().advanceSecondaryJob(job.jobId, event.id);
              const progress = describeModelProgress(event, job.model);
              if (progress) {
                recordProgress(cancelled() ? { ...progress, status: "Cancelling...", cancelling: true } : progress);
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

        if (tracking || secondaryTracking) {
          const current = currentProgress();
          recordProgress({
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
          if (tracking || secondaryTracking) recordProgress({ model: job.model, status: "Cancelled.", percent: null });
          notify(job.kind === "gguf_download" ? "Download cancelled." : "Model operation cancelled.", "info");
          return { succeeded: false, data: null, failure: null, cancelled: true, detached: false };
        }
        const failure = failureMessage
          ?? (terminalStatus === "cancelled" ? "Model operation was cancelled." : "Model operation failed.");
        notify(failure, "error");
        return { succeeded: false, data: null, failure, cancelled: false, detached: false };
      }
      // The worker has already reported a terminal success; inventory lookup
      // is an independent, fallible read and must not turn it into a failed
      // download or suppress one-time GGUF completion handling.
      settled = true;
      const refreshGeneration = ++inventoryRequestGeneration.current;
      let refreshedModels: ModelResponse | null = null;
      try {
        refreshedModels = await api.models();
      } catch {
        if (attached()) {
          notify("Model operation completed, but the model list could not be refreshed. Rescan models to update it.", "info");
        }
      }
      if (!attached()) return DETACHED;
      if (refreshedModels !== null && refreshGeneration === inventoryRequestGeneration.current) {
        onModels(refreshedModels);
        if (options.checkOllamaConnection && !refreshedModels.connection?.success) {
          notify(refreshedModels.connection?.message ?? "Cortex could not reach Ollama.", "error");
        } else if (options.notifyOnSuccess) {
          notify(job.model === "local model inventory" ? "Local model inventory refreshed." : "Model operation completed.", "success");
        }
      } else if (refreshedModels !== null && options.notifyOnSuccess) {
        // A newer snapshot has already taken precedence; the job itself did
        // succeed, but claiming that this old inventory was applied is wrong.
        notify("Model operation completed.", "success");
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
        const refreshGeneration = ++inventoryRequestGeneration.current;
        void api.models().then((models) => {
          if (alive.current && refreshGeneration === inventoryRequestGeneration.current) {
            latest.current.onModels(models);
          }
        }).catch(() => undefined);
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
          if (settled) {
            current.setActiveJob(null);
            // When the primary ends before the secondary, do not show the
            // completed primary row as a still-busy operation.
            if (Object.keys(current.secondaryJobs).length) current.setModelProgress(null);
          }
          current.setModelBusy(Object.keys(current.secondaryJobs).length > 0);
        }
      } else if (secondaryTracking) {
        if (untracked.current.get(job.jobId) === secondaryTracking) untracked.current.delete(job.jobId);
        if (attached()) {
          const current = useModelStore.getState();
          if (settled) current.forgetSecondaryJob(job.jobId);
          const remaining = useModelStore.getState();
          remaining.setModelBusy(Boolean(remaining.activeJob) || Object.keys(remaining.secondaryJobs).length > 0);
        }
      }
    }
  }, []);

  const followOnce = useCallback((
    job: ActiveModelJob,
    options: Required<Pick<ModelJobOptions, "checkOllamaConnection" | "notifyOnSuccess">>,
  ): Promise<ModelJobResult> => {
    const existing = inFlightFollowers.current.get(job.jobId);
    if (existing) return existing;
    // follow() synchronously records the job before yielding on its stream.
    const running = follow(job, options);
    inFlightFollowers.current.set(job.jobId, running);
    const done = () => {
      if (inFlightFollowers.current.get(job.jobId) === running) {
        inFlightFollowers.current.delete(job.jobId);
      }
    };
    void running.then(done, done);
    return running;
  }, [follow]);

  const run = useCallback((accepted: JobAccepted, label = "local model inventory", options: ModelJobOptions = {}) => followOnce(
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
  ), [followOnce]);

  /** Pick a job back up after a reload or a remount, and finish what its caller would have. */
  const attach = useCallback(async (job: ActiveModelJob) => {
    const download = job.kind === "gguf_download";
    const result = await followOnce(job, { checkOllamaConnection: !download, notifyOnSuccess: !download });
    if (!download || !result.succeeded) return;
    const filename = typeof result.data?.filename === "string" ? result.data.filename : null;
    if (filename) await latest.current.onDownloaded(
      filename, job.selectedChatModelAtStart, job.ggufDirectoryAtStart,
    );
  }, [followOnce]);

  const cancel = useCallback(async (jobId?: string) => {
    const primary = tracked.current;
    const tracking = jobId
      ? primary?.jobId === jobId ? primary : untracked.current.get(jobId)
      : primary ?? untracked.current.values().next().value;
    if (!tracking || tracking.cancelRequested) return;
    const isPrimary = primary === tracking;
    // Mutate only the owned tracking ref, never an aliased hook value.
    if (isPrimary && tracked.current) tracked.current.cancelRequested = true;
    else {
      const secondary = untracked.current.get(tracking.jobId);
      if (secondary) secondary.cancelRequested = true;
    }
    const store = useModelStore.getState();
    const before = isPrimary ? store.modelProgress : store.secondaryProgress[tracking.jobId] ?? null;
    const setProgress = (progress: ModelProgress | null) => {
      if (isPrimary) store.setModelProgress(progress);
      else store.setSecondaryProgress(tracking.jobId, progress);
    };
    if (before) setProgress({ ...before, status: "Cancelling...", cancelling: true });
    try {
      await latest.current.api.cancelJob(tracking.jobId);
    } catch (error) {
      if (isPrimary && tracked.current?.jobId === tracking.jobId) tracked.current.cancelRequested = false;
      else {
        const secondary = untracked.current.get(tracking.jobId);
        if (secondary) secondary.cancelRequested = false;
      }
      // Restore only this job's row; another concurrent job is unaffected.
      const now = useModelStore.getState();
      const visible = isPrimary ? now.modelProgress : now.secondaryProgress[tracking.jobId];
      if (visible?.cancelling) setProgress(before);
      latest.current.notify(describeApiError(error, "Could not cancel the model operation."), "error");
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    const current = useModelStore.getState();
    const remembered = current.activeJob ?? readStoredModelJob();
    if (remembered && tracked.current === null) {
      void attach(remembered).catch(() => {
        // Picking the job up again is best effort; the job is unaffected.
      });
    }
    // Restore every job the backend admitted, not just the first one that
    // occupied the progress row. The backend allows separate models/GGUF kinds.
    const secondary = Object.values(current.secondaryJobs);
    const extras = secondary.length ? secondary : readStoredSecondaryModelJobs();
    for (const job of extras) {
      if (job.jobId !== remembered?.jobId) {
        void attach(job).catch(() => {
          // Reconciliation is best effort; the backend job remains durable.
        });
      }
    }
    const followers = untracked.current;
    return () => {
      alive.current = false;
      ++inventoryRequestGeneration.current;
      tracked.current?.controller.abort();
      tracked.current = null;
      followers.forEach((tracking) => tracking.controller.abort());
      followers.clear();
      // The job itself is durable on the backend and stays remembered, but
      // this screen's ownership of it ends here. Do not strand a busy flag in
      // the process-wide store after logout or a remount.
      const store = useModelStore.getState();
      store.setModelBusy(false);
      store.setModelProgress(null);
      for (const jobId of Object.keys(store.secondaryProgress)) store.setSecondaryProgress(jobId, null);
    };
  }, [attach]);

  return useMemo(() => ({ run, cancel }), [run, cancel]);
}
