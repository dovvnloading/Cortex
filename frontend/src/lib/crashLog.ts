/**
 * The last UI crash, kept for the browser session so it can still be copied
 * after the view that crashed is gone.
 *
 * Local only: the record goes to session storage and nowhere else. It holds
 * where the crash happened, the error's name and message, and the React
 * component stack -- component names and bundle locations. It never holds
 * prompts, responses, memories, props, store contents, the URL or an error's
 * `cause`, because none of those are read here. The message is the one field
 * that comes from running code, so it is bounded and flattened to one line;
 * code that throws must keep conversation text out of its error messages.
 */

export const LAST_CRASH_KEY = "cortex.last-crash";

const NAME_LIMIT = 80;
const SCOPE_LIMIT = 40;
const MESSAGE_LIMIT = 300;
const STACK_LINE_LIMIT = 25;
const STACK_LIMIT = 2000;

export type CrashRecord = {
  time: string;
  scope: string;
  name: string;
  message: string;
  componentStack: string;
};

// C0 and C1 control characters other than the line breaks a stack legitimately
// has. Replaced rather than stripped so words either side of one stay apart.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/g;

function oneLine(value: unknown, limit: number): string {
  if (typeof value !== "string") return "";
  return value.replace(CONTROL_CHARACTERS, " ").replace(/\s+/g, " ").trim().slice(0, limit);
}

function stackLines(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .replace(CONTROL_CHARACTERS, " ")
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "")
    .slice(0, STACK_LINE_LIMIT)
    .join("\n")
    .slice(0, STACK_LIMIT);
}

/** Build the record for a caught error. Pure: nothing is written. */
export function buildCrashRecord(
  error: unknown,
  componentStack: string | null | undefined,
  scope: string,
  now: Date = new Date(),
): CrashRecord {
  // Anything can be thrown. Only a real error's own name and message are read;
  // an arbitrary thrown value is never turned into text, since it could be
  // anything the app was holding.
  const isError = typeof error === "object" && error !== null;
  const named = isError ? (error as { name?: unknown }).name : undefined;
  const message = isError ? (error as { message?: unknown }).message : undefined;
  return {
    time: now.toISOString(),
    scope: oneLine(scope, SCOPE_LIMIT) || "app",
    name: oneLine(named, NAME_LIMIT) || "Error",
    message: oneLine(message, MESSAGE_LIMIT),
    componentStack: stackLines(componentStack),
  };
}

/** Remember a crash for this session. Best effort: storage may be unavailable. */
export function recordCrash(
  error: unknown,
  componentStack: string | null | undefined,
  scope: string,
): CrashRecord {
  const record = buildCrashRecord(error, componentStack, scope);
  try {
    window.sessionStorage.setItem(LAST_CRASH_KEY, JSON.stringify(record));
  } catch {
    // The crash screen still works; only "copy details after a reload" is lost.
  }
  return record;
}

function isCrashRecord(value: unknown): value is CrashRecord {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return ["time", "scope", "name", "message", "componentStack"].every(
    (field) => typeof candidate[field] === "string",
  );
}

export function readLastCrash(): CrashRecord | null {
  try {
    const raw = window.sessionStorage.getItem(LAST_CRASH_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    return isCrashRecord(value) ? value : null;
  } catch {
    return null;
  }
}

/** The text a person pastes into a bug report. */
export function describeCrash(record: CrashRecord): string {
  return [
    "Cortex UI crash",
    `Time: ${record.time}`,
    `View: ${record.scope}`,
    `Error: ${record.name}${record.message ? `: ${record.message}` : ""}`,
    ...(record.componentStack ? ["Component stack:", record.componentStack] : []),
  ].join("\n");
}
