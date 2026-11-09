/** Recover an Ollama pull when its HTTP 202 acknowledgment is lost. */
export const OLLAMA_PULL_ADMISSION_KEY = "cortex.pending.ollama-pull";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

export type PendingOllamaPullAdmission = {
  model: string;
  requestId: string;
  createdAt: number;
};

let volatileAdmission: PendingOllamaPullAdmission | null = null;
let storageUnavailable = false;

function isAdmission(value: unknown): value is PendingOllamaPullAdmission {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Partial<PendingOllamaPullAdmission>;
  return typeof candidate.model === "string"
    && candidate.model.length > 0
    && candidate.model === candidate.model.trim()
    && typeof candidate.requestId === "string"
    && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(candidate.requestId)
    && typeof candidate.createdAt === "number"
    && Number.isFinite(candidate.createdAt)
    && candidate.createdAt > 0
    && candidate.createdAt <= Date.now()
    && Date.now() - candidate.createdAt < MAX_AGE_MS;
}

function storeAdmission(pending: PendingOllamaPullAdmission | null): void {
  volatileAdmission = pending;
  try {
    if (pending) window.sessionStorage.setItem(OLLAMA_PULL_ADMISSION_KEY, JSON.stringify(pending));
    else window.sessionStorage.removeItem(OLLAMA_PULL_ADMISSION_KEY);
    storageUnavailable = false;
  } catch {
    storageUnavailable = true;
    // Keep an in-memory fallback if the browser denies session storage.
  }
}

export function readPendingOllamaPullAdmission(): PendingOllamaPullAdmission | null {
  try {
    const raw = window.sessionStorage.getItem(OLLAMA_PULL_ADMISSION_KEY);
    if (raw) {
      const decoded: unknown = JSON.parse(raw);
      if (isAdmission(decoded)) {
        volatileAdmission = decoded;
        return decoded;
      }
      window.sessionStorage.removeItem(OLLAMA_PULL_ADMISSION_KEY);
      volatileAdmission = null;
      return null;
    }
    if (!storageUnavailable) {
      // Explicit storage clearing must not resurrect an old volatile request.
      volatileAdmission = null;
      return null;
    }
  } catch {
    // Blocked storage or corrupt JSON: use the guarded in-memory fallback.
  }
  return isAdmission(volatileAdmission) ? volatileAdmission : null;
}

export function beginOllamaPullAdmission(model: string): PendingOllamaPullAdmission {
  const exactModel = model.trim();
  if (!exactModel) throw new Error("Select a valid Ollama model before downloading.");
  const existing = readPendingOllamaPullAdmission();
  if (existing) {
    if (existing.model !== exactModel) {
      throw new Error(
        "A previous Ollama pull has not been confirmed. Retry that model before starting another.",
      );
    }
    return existing;
  }

  const requestId = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `ollama-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const pending = { model: exactModel, requestId, createdAt: Date.now() };
  storeAdmission(pending);
  return pending;
}

export function clearOllamaPullAdmission(requestId: string): void {
  const current = readPendingOllamaPullAdmission();
  if (current?.requestId === requestId) storeAdmission(null);
}
