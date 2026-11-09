import { create } from "zustand";
import type { LlamaCppRuntimeStatus, ModelResponse } from "../../../contracts/cortex-api";

export type ModelProgress = {
  model: string;
  status: string;
  percent: number | null;
  /** The person asked for the operation to stop and it has not yet. */
  cancelling?: boolean;
};

/** The two kinds of job the backend runs for model work (`JobKind` minus generations). */
export type ModelJobKind = "models" | "gguf_download";

/**
 * A model job this browser tab is following. The job itself lives on the
 * backend and outlives the page, so this is only what is needed to find it
 * again: which job, what to call it, and the last event already shown.
 */
export type ActiveModelJob = {
  jobId: string;
  kind: ModelJobKind;
  model: string;
  lastEventId: number;
  /** The chat model selected when this GGUF download began; missing on legacy jobs. */
  selectedChatModelAtStart?: string | null;
  /** GGUF folder at admission; missing for older stored jobs. */
  ggufDirectoryAtStart?: string | null;
};

const ACTIVE_MODEL_JOB_KEY = "cortex.active.model-job";
const SECONDARY_JOBS_KEY = "cortex.active.model-jobs.secondary";

function isActiveModelJob(value: unknown): value is ActiveModelJob {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ActiveModelJob>;
  return typeof candidate.jobId === "string"
    && candidate.jobId.length > 0
    && (candidate.kind === "models" || candidate.kind === "gguf_download")
    && typeof candidate.model === "string"
    && typeof candidate.lastEventId === "number"
    && Number.isSafeInteger(candidate.lastEventId)
    && candidate.lastEventId >= 0
    && (
      !("selectedChatModelAtStart" in candidate)
      || candidate.selectedChatModelAtStart === null
      || typeof candidate.selectedChatModelAtStart === "string"
    )
    && (
      !("ggufDirectoryAtStart" in candidate)
      || candidate.ggufDirectoryAtStart === null
      || typeof candidate.ggufDirectoryAtStart === "string"
    );
}

/**
 * The job a previous page load was following, if the tab still remembers one.
 *
 * Session storage is a best-effort mirror of the store, never the source of
 * truth: a browser that denies it, or a value that is not what this file
 * wrote, means only that a reload forgets the download's progress.
 */
export function readStoredModelJob(): ActiveModelJob | null {
  try {
    const raw = window.sessionStorage.getItem(ACTIVE_MODEL_JOB_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    return isActiveModelJob(value) ? value : null;
  } catch {
    return null;
  }
}

function storeModelJob(job: ActiveModelJob | null): void {
  try {
    if (job) window.sessionStorage.setItem(ACTIVE_MODEL_JOB_KEY, JSON.stringify(job));
    else window.sessionStorage.removeItem(ACTIVE_MODEL_JOB_KEY);
  } catch {
    // The store still knows the job; only surviving a reload is lost.
  }
}

/** Extra model operations are independent backend job kinds. Both must survive reload. */
export function readStoredSecondaryModelJobs(): ActiveModelJob[] {
  try {
    const raw = window.sessionStorage.getItem(SECONDARY_JOBS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const jobs: ActiveModelJob[] = [];
    for (const candidate of parsed.slice(0, 2) as unknown[]) {
      if (isActiveModelJob(candidate) && !jobs.some((job) => job.jobId === candidate.jobId)) {
        jobs.push(candidate);
      }
    }
    return jobs;
  } catch {
    return [];
  }
}

function storeSecondaryJobs(jobs: Record<string, ActiveModelJob>): void {
  try {
    const values = Object.values(jobs);
    if (values.length) window.sessionStorage.setItem(SECONDARY_JOBS_KEY, JSON.stringify(values));
    else window.sessionStorage.removeItem(SECONDARY_JOBS_KEY);
  } catch {
    // The in-memory followers and their Cancel controls still work.
  }
}

interface ModelStoreState {
  models: ModelResponse | null;
  modelBusy: boolean;
  modelProgress: ModelProgress | null;
  activeJob: ActiveModelJob | null;
  /** Every concurrent non-primary operation, indexed by durable backend job ID. */
  secondaryJobs: Record<string, ActiveModelJob>;
  secondaryProgress: Record<string, ModelProgress>;
  llamacppStatus: LlamaCppRuntimeStatus | null;
  setModels: (models: ModelResponse | null) => void;
  setModelBusy: (busy: boolean) => void;
  setModelProgress: (progress: ModelProgress | null) => void;
  /** Remember (or, with `null`, forget) the job being followed, here and in session storage. */
  setActiveJob: (job: ActiveModelJob | null) => void;
  /** Note the last event of `jobId` that was shown; ignored once another job is being followed. */
  advanceActiveJob: (jobId: string, lastEventId: number) => void;
  rememberSecondaryJob: (job: ActiveModelJob) => void;
  advanceSecondaryJob: (jobId: string, lastEventId: number) => void;
  setSecondaryProgress: (jobId: string, progress: ModelProgress | null) => void;
  forgetSecondaryJob: (jobId: string) => void;
  clearSecondaryJobs: () => void;
  setLlamacppStatus: (status: LlamaCppRuntimeStatus | null) => void;
}

export const useModelStore = create<ModelStoreState>((set, get) => ({
  models: null,
  modelBusy: false,
  modelProgress: null,
  activeJob: null,
  secondaryJobs: {},
  secondaryProgress: {},
  llamacppStatus: null,
  setModels: (models) => set({ models }),
  setModelBusy: (modelBusy) => set({ modelBusy }),
  setModelProgress: (modelProgress) => set({ modelProgress }),
  setActiveJob: (activeJob) => {
    storeModelJob(activeJob);
    set({ activeJob });
  },
  advanceActiveJob: (jobId, lastEventId) => {
    const current = get().activeJob;
    if (!current || current.jobId !== jobId || lastEventId <= current.lastEventId) return;
    const activeJob = { ...current, lastEventId };
    storeModelJob(activeJob);
    set({ activeJob });
  },
  rememberSecondaryJob: (job) => {
    const secondaryJobs = { ...get().secondaryJobs, [job.jobId]: job };
    storeSecondaryJobs(secondaryJobs);
    set({ secondaryJobs });
  },
  advanceSecondaryJob: (jobId, lastEventId) => {
    const current = get().secondaryJobs[jobId];
    if (!current || lastEventId <= current.lastEventId) return;
    const secondaryJobs = { ...get().secondaryJobs, [jobId]: { ...current, lastEventId } };
    storeSecondaryJobs(secondaryJobs);
    set({ secondaryJobs });
  },
  setSecondaryProgress: (jobId, progress) => set((current) => {
    const secondaryProgress = { ...current.secondaryProgress };
    if (progress) secondaryProgress[jobId] = progress;
    else delete secondaryProgress[jobId];
    return { secondaryProgress };
  }),
  forgetSecondaryJob: (jobId) => {
    const secondaryJobs = { ...get().secondaryJobs };
    if (!(jobId in secondaryJobs)) return;
    delete secondaryJobs[jobId];
    const secondaryProgress = { ...get().secondaryProgress };
    delete secondaryProgress[jobId];
    storeSecondaryJobs(secondaryJobs);
    set({ secondaryJobs, secondaryProgress });
  },
  clearSecondaryJobs: () => {
    storeSecondaryJobs({});
    set({ secondaryJobs: {}, secondaryProgress: {} });
  },
  setLlamacppStatus: (llamacppStatus) => set({ llamacppStatus }),
}));
