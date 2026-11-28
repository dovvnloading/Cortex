import type { ChatAttachment, GenerationOptionsOverride } from "../../../contracts/cortex-api";

/** The exact admission intent needed to replay an ambiguous POST after route unmount/reload. */
export type PendingGenerationAdmission = {
  requestId: string;
  operation: "generate" | "regenerate";
  threadId: string | null;
  baseRevision?: number;
  expectedModel?: string;
  options?: GenerationOptionsOverride;
  messageId?: string;
  prompt: string;
  attachments: ChatAttachment[];
  createdAt: number;
};

const KEY = "cortex.pending.generation-admission";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;
let memoryFallback: PendingGenerationAdmission | null = null;
// Every mounted chat page listens: one that was mounted when the admission
// was settled by another (a Settings round trip) must not keep a stale copy
// and block the next send (#686).
const listeners = new Set<() => void>();
let snapshotRaw: string | null | undefined;
let snapshotValue: PendingGenerationAdmission | null = null;

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function subscribePendingGenerationAdmission(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function rawPending(): string | null {
  if (memoryFallback) return JSON.stringify(memoryFallback);
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** The current pending admission, the same object while it is unchanged (for useSyncExternalStore). */
export function pendingGenerationAdmissionSnapshot(): PendingGenerationAdmission | null {
  const raw = rawPending();
  if (raw !== snapshotRaw) {
    snapshotValue = readPendingGenerationAdmission({ quiet: true });
    snapshotRaw = rawPending();
  }
  return snapshotValue;
}

function isPending(value: unknown): value is PendingGenerationAdmission {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const v = value as Partial<PendingGenerationAdmission>;
  return typeof v.requestId === "string"
    && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(v.requestId)
    && (v.operation === "generate" || v.operation === "regenerate")
    && (v.threadId === null || typeof v.threadId === "string")
    && (v.baseRevision === undefined || (Number.isSafeInteger(v.baseRevision) && v.baseRevision >= 0))
    && (v.expectedModel === undefined || (typeof v.expectedModel === "string" && v.expectedModel.length > 0 && v.expectedModel.length <= 2048))
    && (v.options === undefined || (v.options !== null && typeof v.options === "object" && !Array.isArray(v.options)))
    && (v.operation === "generate" || (typeof v.messageId === "string" && v.messageId.length > 0))
    && typeof v.prompt === "string"
    && v.prompt.length > 0
    && v.prompt.length <= 100_000
    && Array.isArray(v.attachments)
    && v.attachments.length <= 8
    && v.attachments.every((attachment) => attachment
      && typeof attachment === "object"
      && typeof attachment.attachment_id === "string"
      && typeof attachment.filename === "string")
    && typeof v.createdAt === "number"
    && Number.isFinite(v.createdAt)
    && v.createdAt > 0;
}

export function readPendingGenerationAdmission(
  { quiet = false }: { quiet?: boolean } = {},
): PendingGenerationAdmission | null {
  let pending: unknown = memoryFallback;
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw) pending = JSON.parse(raw) as unknown;
  } catch {
    // In-memory fallback survives a page unmount when session storage is denied.
  }
  if (!isPending(pending) || Date.now() - pending.createdAt > MAX_AGE_MS || pending.createdAt > Date.now() + 60_000) {
    // A render-time read drops the stale record without telling listeners.
    discard();
    if (!quiet) notify();
    return null;
  }
  return pending;
}

export function storePendingGenerationAdmission(pending: PendingGenerationAdmission): void {
  memoryFallback = null;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(pending));
  } catch {
    memoryFallback = pending;
  }
  notify();
}

function discard(): void {
  memoryFallback = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // The in-memory copy is already cleared.
  }
}

export function clearPendingGenerationAdmission(): void {
  discard();
  notify();
}
