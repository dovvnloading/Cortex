import type { GenerationOptionsOverride } from "../../../contracts/cortex-api";
import { PARAM_SPECS, type ParamKey } from "./generationParams";

/** Only numeric per-chat preferences, never prompts, history, or credentials. */
export const PERSISTED_GENERATION_OPTIONS_KEY = "cortex.chat.generation-options.v1";
const MAX_THREAD_OPTIONS = 100;
const MAX_STORED_CHARS = 32_000;
const THREAD_ID = /^[A-Za-z0-9_-]{1,128}$/;

/** Reject malformed or out-of-range storage values before they reach the model. */
function checkedOptions(value: unknown): GenerationOptionsOverride | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries = Object.entries(value);
  if (!entries.length || entries.length > Object.keys(PARAM_SPECS).length) return null;
  const next: GenerationOptionsOverride = {};
  for (const [key, item] of entries) {
    if (!Object.hasOwn(PARAM_SPECS, key)) return null;
    const spec = PARAM_SPECS[key as ParamKey];
    if (
      typeof item !== "number" || !Number.isFinite(item)
      || item < spec.min || item > spec.max
      || (spec.decimals === 0 && !Number.isInteger(item))
    ) return null;
    next[key as ParamKey] = item;
  }
  return next;
}

function validThreadId(thread: string): boolean {
  // The unsaved New thread has no durable server identifier yet.
  return thread !== "new" && THREAD_ID.test(thread);
}

export function readPersistedGenerationOptions(): Record<string, GenerationOptionsOverride> {
  try {
    const raw = window.localStorage.getItem(PERSISTED_GENERATION_OPTIONS_KEY);
    if (!raw || raw.length > MAX_STORED_CHARS) return {};
    const payload: unknown = JSON.parse(raw);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    const record = payload as { version?: unknown; options?: unknown };
    if (record.version !== 1 || !record.options || typeof record.options !== "object" || Array.isArray(record.options)) return {};
    const entries = Object.entries(record.options);
    if (entries.length > MAX_THREAD_OPTIONS) return {};
    const safe: Record<string, GenerationOptionsOverride> = {};
    for (const [thread, value] of entries) {
      const checked = checkedOptions(value);
      if (validThreadId(thread) && checked) safe[thread] = checked;
    }
    return safe;
  } catch {
    // Private browsing, denied storage, or damaged JSON cannot block chat.
    return {};
  }
}

/**
 * Store at most the most recently updated 100 conversations' overrides.
 * Failure to persist is non-fatal; the in-memory Zustand state still works.
 */
export function writePersistedGenerationOptions(values: Record<string, GenerationOptionsOverride>): void {
  const clean = Object.entries(values).flatMap(([thread, value]) => {
    const options = checkedOptions(value);
    return validThreadId(thread) && options ? [[thread, options] as const] : [];
  }).slice(-MAX_THREAD_OPTIONS);
  try {
    if (clean.length) {
      window.localStorage.setItem(PERSISTED_GENERATION_OPTIONS_KEY, JSON.stringify({
        version: 1, options: Object.fromEntries(clean),
      }));
    } else {
      window.localStorage.removeItem(PERSISTED_GENERATION_OPTIONS_KEY);
    }
  } catch {
    // When local storage is unavailable, keep the current conversation usable.
  }
}

/** Remove preferences of server-deleted chats after loading the full chat list. */
export function prunePersistedGenerationOptions(existingThreadIds: Iterable<string>): void {
  const existing = new Set(existingThreadIds);
  const persisted = readPersistedGenerationOptions();
  writePersistedGenerationOptions(Object.fromEntries(
    Object.entries(persisted).filter(([thread]) => existing.has(thread)),
  ));
}
