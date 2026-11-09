import type { ModelDownloadRequest } from "../../../contracts/cortex-api";

/**
 * The HTTP 202 for a multi-GB GGUF job can be lost after the backend has
 * accepted it. Save the *request* before sending it, not the eventual job ID,
 * so a retry/reload can ask for the same idempotent job instead of orphaning it.
 * This is scoped to the browser tab, never written to the persisted Settings.
 */
export const GGUF_ADMISSION_KEY = "cortex.pending.gguf-admission";
const ADMISSION_TTL_MS = 24 * 60 * 60 * 1000;

export type PendingGGUFAdmission = {
  payload: ModelDownloadRequest & { request_id: string };
  selectedChatModelAtStart: string | null;
  ggufDirectoryAtStart: string | null;
  createdAt: number;
};

let volatileAdmission: PendingGGUFAdmission | null = null;
let storageUnavailable = false;

function isAdmission(value: unknown): value is PendingGGUFAdmission {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const admission = value as Partial<PendingGGUFAdmission>;
  const payload = admission.payload as Partial<ModelDownloadRequest> | undefined;
  return !!payload
    && typeof payload.request_id === "string"
    && payload.request_id.length > 0
    && payload.request_id.length <= 200
    && (payload.source === "url" || payload.source === "huggingface")
    && (payload.url === undefined || payload.url === null || typeof payload.url === "string")
    && (payload.repo_id === undefined || payload.repo_id === null || typeof payload.repo_id === "string")
    && (payload.filename === undefined || payload.filename === null || typeof payload.filename === "string")
    && (admission.selectedChatModelAtStart === null || typeof admission.selectedChatModelAtStart === "string")
    && (admission.ggufDirectoryAtStart === null || typeof admission.ggufDirectoryAtStart === "string")
    && typeof admission.createdAt === "number"
    && Number.isFinite(admission.createdAt)
    && admission.createdAt <= Date.now()
    && Date.now() - admission.createdAt < ADMISSION_TTL_MS;
}

function equivalent(left: ModelDownloadRequest, right: ModelDownloadRequest): boolean {
  return left.source === right.source
    && (left.url ?? null) === (right.url ?? null)
    && (left.repo_id ?? null) === (right.repo_id ?? null)
    && (left.filename ?? null) === (right.filename ?? null);
}

function storePending(value: PendingGGUFAdmission | null): void {
  volatileAdmission = value;
  try {
    if (value) window.sessionStorage.setItem(GGUF_ADMISSION_KEY, JSON.stringify(value));
    else window.sessionStorage.removeItem(GGUF_ADMISSION_KEY);
    storageUnavailable = false;
  } catch {
    storageUnavailable = true;
    // The tab still remembers this request in memory if storage is disabled.
  }
}

export function readPendingGGUFAdmission(): PendingGGUFAdmission | null {
  try {
    const raw = window.sessionStorage.getItem(GGUF_ADMISSION_KEY);
    if (raw) {
      const decoded: unknown = JSON.parse(raw);
      if (isAdmission(decoded)) {
        volatileAdmission = decoded;
        return decoded;
      }
      window.sessionStorage.removeItem(GGUF_ADMISSION_KEY);
      volatileAdmission = null;
      return null;
    }
    if (!storageUnavailable) {
      // Explicitly cleared session storage invalidates the volatile mirror;
      // don't resurrect a stale pending request on a future user action.
      volatileAdmission = null;
      return null;
    }
  } catch {
    // Denied storage or invalid JSON; fall through to the in-memory copy.
  }
  return isAdmission(volatileAdmission) ? volatileAdmission : null;
}

export function beginGGUFAdmission(
  request: ModelDownloadRequest,
  selectedChatModelAtStart: string | null,
  ggufDirectoryAtStart: string | null,
): PendingGGUFAdmission {
  const pending = readPendingGGUFAdmission();
  if (pending) {
    if (!equivalent(pending.payload, request)) {
      throw new Error(
        "A previous model download has not been confirmed. Retry that download before starting another.",
      );
    }
    return pending;
  }

  const requestId = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  const admission: PendingGGUFAdmission = {
    payload: { ...request, request_id: requestId },
    selectedChatModelAtStart,
    ggufDirectoryAtStart,
    createdAt: Date.now(),
  };
  storePending(admission);
  return admission;
}

export function clearGGUFAdmission(requestId: string): void {
  const current = readPendingGGUFAdmission();
  if (current?.payload.request_id === requestId) storePending(null);
}
