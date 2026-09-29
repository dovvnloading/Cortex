/**
 * The last UI crash, kept for the browser session so it can still be copied
 * after the view that crashed is gone.
 *
 * Local only: the record goes to session storage and nowhere else. It holds
 * only what the code can vouch for: when it happened, which part of the app
 * caught it (a constant the app passes in), the error's class name if it is one
 * of a short known list, and the names of the React components it happened in.
 *
 * It holds no error text. A message comes from running code, which can build it
 * from anything it was holding -- the app itself puts file names into some --
 * and nothing can decide from the outside that one is free of conversation
 * text, so none is read. Nor are prompts, responses, memories, props, store
 * contents, the URL, an error's `cause`, stack frames or bundle locations.
 */

export const LAST_CRASH_KEY = "cortex.last-crash";

const SCOPE_LIMIT = 40;
const COMPONENT_NAME_LIMIT = 60;
const COMPONENT_PATH_LENGTH = 25;

// An error's name is read from running code too, so only these are believed:
// the ones the platform defines, and the app's own transport error.
const KNOWN_ERROR_NAMES: ReadonlySet<string> = new Set([
  "Error",
  "TypeError",
  "RangeError",
  "ReferenceError",
  "SyntaxError",
  "EvalError",
  "URIError",
  "AggregateError",
  "ApiError",
]);

export type CrashRecord = {
  time: string;
  scope: string;
  name: string;
  /** Component names, innermost first, joined with " > ". No locations. */
  componentPath: string;
};

// C0 and C1 control characters. Replaced rather than stripped so words either
// side of one stay apart.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/g;

// A component name is an identifier, optionally dotted (Context.Consumer). Only
// a line that is shaped like a stack frame contributes one: "at Name (" in V8,
// "Name@" in Gecko and WebKit. Anything else on a line is not read.
const COMPONENT_NAME = String.raw`([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)`;
const V8_FRAME = new RegExp(String.raw`^\s*at\s+(?:new\s+)?${COMPONENT_NAME}\s+\(`);
const GECKO_FRAME = new RegExp(String.raw`^\s*${COMPONENT_NAME}@`);

/** The scope is a constant the app passes in; it is bounded and flattened anyway. */
function oneLine(value: unknown, limit: number): string {
  if (typeof value !== "string") return "";
  return value.replace(CONTROL_CHARACTERS, " ").replace(/\s+/g, " ").trim().slice(0, limit);
}

/** The error's class name if it is a known one, "Error" otherwise. */
function errorName(error: unknown): string {
  const named = typeof error === "object" && error !== null ? (error as { name?: unknown }).name : undefined;
  return typeof named === "string" && KNOWN_ERROR_NAMES.has(named) ? named : "Error";
}

/** Component names from a React component stack, innermost first, without locations. */
function componentNames(value: unknown): string {
  if (typeof value !== "string") return "";
  const names: string[] = [];
  for (const line of value.split(/\r?\n/)) {
    const name = (V8_FRAME.exec(line) ?? GECKO_FRAME.exec(line))?.[1];
    if (name && name.length <= COMPONENT_NAME_LIMIT) names.push(name);
    if (names.length === COMPONENT_PATH_LENGTH) break;
  }
  return names.join(" > ");
}

/** Build the record for a caught error. Pure: nothing is written. */
export function buildCrashRecord(
  error: unknown,
  componentStack: string | null | undefined,
  scope: string,
  now: Date = new Date(),
): CrashRecord {
  return {
    time: now.toISOString(),
    scope: oneLine(scope, SCOPE_LIMIT) || "app",
    name: errorName(error),
    componentPath: componentNames(componentStack),
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
  return ["time", "scope", "name", "componentPath"].every(
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
    `Error type: ${record.name}`,
    ...(record.componentPath ? [`Components, innermost first: ${record.componentPath}`] : []),
  ].join("\n");
}
