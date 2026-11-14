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

export function readPendingGenerationAdmission(): PendingGenerationAdmission | null {
  let pending: unknown = memoryFallback;
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw) pending = JSON.parse(raw) as unknown;
  } catch {
    // In-memory fallback survives a page unmount when session storage is denied.
  }
  if (!isPending(pending) || Date.now() - pending.createdAt > MAX_AGE_MS || pending.createdAt > Date.now() + 60_000) {
    clearPendingGenerationAdmission();
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
}

export function clearPendingGenerationAdmission(): void {
  memoryFallback = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // The in-memory copy is already cleared.
  }
}
