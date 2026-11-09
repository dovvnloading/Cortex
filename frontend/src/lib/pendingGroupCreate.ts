/** Keep a chat-group POST identity until its outcome is unambiguous. */
export const PENDING_GROUP_CREATE_KEY = "cortex.pending.group-create";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

export type PendingGroupCreate = {
  name: string;
  requestId: string;
  createdAt: number;
};

let volatileIntent: PendingGroupCreate | null = null;
let storageUnavailable = false;

function validIntent(value: unknown): value is PendingGroupCreate {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Partial<PendingGroupCreate>;
  return typeof item.name === "string"
    && item.name.length > 0 && item.name.length <= 120
    && item.name === item.name.trim()
    && typeof item.requestId === "string"
    && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(item.requestId)
    && typeof item.createdAt === "number"
    && Number.isFinite(item.createdAt)
    && item.createdAt <= Date.now()
    && Date.now() - item.createdAt < MAX_AGE_MS;
}

function store(value: PendingGroupCreate | null): void {
  volatileIntent = value;
  try {
    if (value) window.sessionStorage.setItem(PENDING_GROUP_CREATE_KEY, JSON.stringify(value));
    else window.sessionStorage.removeItem(PENDING_GROUP_CREATE_KEY);
    storageUnavailable = false;
  } catch {
    storageUnavailable = true;
  }
}

export function readPendingGroupCreate(): PendingGroupCreate | null {
  try {
    const stored = window.sessionStorage.getItem(PENDING_GROUP_CREATE_KEY);
    if (stored) {
      const decoded: unknown = JSON.parse(stored);
      if (validIntent(decoded)) {
        volatileIntent = decoded;
        return decoded;
      }
      window.sessionStorage.removeItem(PENDING_GROUP_CREATE_KEY);
      volatileIntent = null;
      return null;
    }
    if (!storageUnavailable) {
      volatileIntent = null;
      return null;
    }
  } catch {
    // A blocked store still retains the pending operation for this JS session.
  }
  return validIntent(volatileIntent) ? volatileIntent : null;
}

export function beginGroupCreate(name: string): PendingGroupCreate {
  const normalized = name.trim();
  if (!normalized || normalized.length > 120) throw new Error("Enter a valid group name.");
  const existing = readPendingGroupCreate();
  if (existing) {
    if (existing.name !== normalized) {
      throw new Error(
        "A previous group creation was not confirmed. Retry that name before creating another.",
      );
    }
    return existing;
  }
  const requestId = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `group-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const intent = { name: normalized, requestId, createdAt: Date.now() };
  store(intent);
  return intent;
}

export function clearGroupCreate(requestId: string): void {
  if (readPendingGroupCreate()?.requestId === requestId) store(null);
}
