import { useCallback, useSyncExternalStore } from "react";

const NAVIGATION_EVENT = "cortex:navigation";

export type AppRoute =
  | { kind: "chat"; threadId: string | null }
  | { kind: "settings" }
  | { kind: "not-found" };

export type NavigateOptions = {
  replace?: boolean;
};

/**
 * Asked before the app moves to another route. Return `true` to allow it and
 * `false` to stay; a promise lets the guard ask the user first. `destination`
 * is the path, query and hash being navigated to.
 */
export type NavigationGuard = (destination: string) => boolean | Promise<boolean>;

const guards = new Set<NavigationGuard>();
// Only the newest navigation may complete: a guard that is still waiting on the
// user must not later push a route the user has since moved away from.
let navigationSerial = 0;

// Browser history traversal changes window.location BEFORE popstate runs. Keep
// the visible route fixed while a guard asks Save/Discard/Keep editing, then
// rewind the browser's history cursor and replay the requested traversal only
// if approved. Indexed app entries let Back and Forward retain their ordinary
// history semantics; neither action pushes a synthetic new entry.
const HISTORY_POSITION = "__cortex_history_position";
let committedLocation: string | null = null;
let committedIndex = 0;
let historyListenerInstalled = false;

type PendingHistory = {
  origin: string;
  originIndex: number;
  target: string;
  targetIndex: number;
  phase: "reverting" | "asking" | "replaying";
  allowed: boolean | null;
  serial: number;
};
let pendingHistory: PendingHistory | null = null;

function locationPath(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

function historyPosition(): number | null {
  const state: unknown = window.history.state;
  if (!state || typeof state !== "object" || Array.isArray(state)) return null;
  const value = (state as Record<string, unknown>)[HISTORY_POSITION];
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function publishRoute(path: string, position: number): void {
  committedLocation = path;
  committedIndex = position;
  window.dispatchEvent(new Event(NAVIGATION_EVENT));
}

function finishPendingHistory(pending: PendingHistory): void {
  if (pendingHistory !== pending || pending.phase !== "asking" || pending.allowed === null) return;
  if (!pending.allowed || pending.serial !== navigationSerial) {
    pendingHistory = null;
    return;
  }
  pending.phase = "replaying";
  window.history.go(pending.targetIndex - pending.originIndex);
}

function handleHistoryPop(): void {
  const path = locationPath();
  const index = historyPosition();
  const waiting = pendingHistory;
  if (waiting) {
    if (waiting.phase === "reverting") {
      if (index === waiting.originIndex) {
        waiting.phase = "asking";
        finishPendingHistory(waiting);
      } else if (index !== null) {
        // Multiple rapid Back or Forward events before our reversal settles:
        // keep the most recent destination while re-entering the visible page.
        waiting.target = path;
        waiting.targetIndex = index;
        window.history.go(waiting.originIndex - index);
      }
      return;
    }
    if (waiting.phase === "replaying") {
      pendingHistory = null;
      publishRoute(path, index ?? waiting.targetIndex);
      return;
    }
    // Another native history action while the shared prompt is open. The
    // current panel stays mounted and the latest destination wins.
    if (index !== null && index !== waiting.originIndex) {
      waiting.target = path;
      waiting.targetIndex = index;
      waiting.phase = "reverting";
      window.history.go(waiting.originIndex - index);
    }
    return;
  }
  const origin = committedLocation ?? path;
  const originIndex = committedIndex;
  if (path === origin) {
    if (index !== null) committedIndex = index;
    return;
  }
  const serial = ++navigationSerial;
  const allowed = checkGuards([...guards], path);
  if (allowed === true) {
    publishRoute(path, index ?? originIndex);
    return;
  }
  if (index === null || index === originIndex) {
    // Unknown external history entry: retain the current guarded route
    // instead of unmounting Settings. Replaying uses a new guarded app entry
    // only when historical cursor positions cannot be recovered.
    window.history.replaceState({ [HISTORY_POSITION]: originIndex }, "", origin);
    const approve = (yes: boolean) => {
      if (yes && navigationSerial === serial) navigate(path);
    };
    if (typeof allowed === "boolean") approve(allowed);
    else void allowed.then(approve);
    return;
  }
  const pending: PendingHistory = {
    origin, originIndex, target: path, targetIndex: index,
    phase: "reverting", allowed: typeof allowed === "boolean" ? allowed : null, serial,
  };
  pendingHistory = pending;
  window.history.go(originIndex - index);
  if (typeof allowed !== "boolean") {
    void allowed.then((ok) => {
      if (pendingHistory !== pending) return;
      pending.allowed = ok;
      finishPendingHistory(pending);
    });
  }
}

function ensureHistoryTracking(): void {
  if (!historyListenerInstalled) {
    window.addEventListener("popstate", handleHistoryPop);
    historyListenerInstalled = true;
  }
  // Also resync when a new app/test host replaces its history without a
  // popstate event, but never discard the visible route during a prompt.
  if (committedLocation === null || (!pendingHistory && guards.size === 0 && committedLocation !== locationPath())) {
    committedLocation = locationPath();
    committedIndex = historyPosition() ?? 0;
    if (historyPosition() === null) {
      const current = window.history.state;
      const state = current && typeof current === "object" && !Array.isArray(current) ? current : {};
      window.history.replaceState({ ...state, [HISTORY_POSITION]: committedIndex }, "", committedLocation);
    }
  }
}

/**
 * Register a guard that can hold back `navigate()`, for example to protect
 * unsaved edits. Returns the function that removes it.
 *
 * Covers app navigation and guarded browser Back/Forward. Native traversal
 * is rolled back until the guard settles; the pending destination is replayed
 * only if the user approves leaving.
 */
export function registerNavigationGuard(guard: NavigationGuard): () => void {
  ensureHistoryTracking();
  guards.add(guard);
  return () => {
    guards.delete(guard);
  };
}

/**
 * Run the guards in order. The result is a plain boolean when every guard
 * answered at once, so an unguarded (or immediately approved) navigation stays
 * synchronous. A guard that throws or rejects is treated as approving: the
 * guards are a convenience, and a broken one must not trap the user on a page.
 */
function checkGuards(pending: readonly NavigationGuard[], destination: string): boolean | Promise<boolean> {
  for (let index = 0; index < pending.length; index += 1) {
    let verdict: boolean | Promise<boolean>;
    try {
      verdict = pending[index](destination);
    } catch {
      continue;
    }
    if (verdict === true) continue;
    if (verdict === false) return false;
    const rest = pending.slice(index + 1);
    return verdict.then(
      (allowed) => (allowed ? checkGuards(rest, destination) : false),
      () => checkGuards(rest, destination),
    );
  }
  return true;
}

function subscribe(listener: () => void): () => void {
  ensureHistoryTracking();
  // Native popstate is handled centrally so Settings cannot unmount while a
  // guard is still considering the historical destination.
  window.addEventListener(NAVIGATION_EVENT, listener);
  return () => window.removeEventListener(NAVIGATION_EVENT, listener);
}

function currentPathname(): string {
  return new URL(committedLocation ?? locationPath(), window.location.origin).pathname;
}

export function usePathname(): string {
  return useSyncExternalStore(subscribe, currentPathname, () => "/chat/new");
}

export function navigate(to: string, options: NavigateOptions = {}): void {
  ensureHistoryTracking();
  if (!to.startsWith("/") || to.startsWith("//")) {
    throw new Error("Cortex navigation only accepts same-origin absolute paths.");
  }

  const destination = new URL(to, window.location.origin);
  if (destination.origin !== window.location.origin) {
    throw new Error("Cortex navigation only accepts same-origin absolute paths.");
  }
  const next = `${destination.pathname}${destination.search}${destination.hash}`;
  if (committedLocation === next) return;

  const commit = () => {
    if (committedLocation === next) return;
    const position = options.replace ? committedIndex : committedIndex + 1;
    window.history[options.replace ? "replaceState" : "pushState"](
      { [HISTORY_POSITION]: position }, "", next,
    );
    publishRoute(next, position);
  };

  const serial = ++navigationSerial;
  const allowed = checkGuards([...guards], next);
  if (typeof allowed === "boolean") {
    if (allowed) commit();
    return;
  }
  void allowed.then((ok) => {
    if (ok && serial === navigationSerial) commit();
  });
}

export function useNavigate(): (to: string, options?: NavigateOptions) => void {
  return useCallback((to: string, options?: NavigateOptions) => navigate(to, options), []);
}

export function parseAppRoute(pathname: string): AppRoute {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (normalized === "/settings") return { kind: "settings" };
  if (normalized === "/" || normalized === "/chat" || normalized === "/chat/new") {
    return { kind: "chat", threadId: null };
  }

  const match = /^\/chat\/([^/]+)$/.exec(normalized);
  if (!match) return { kind: "not-found" };

  try {
    return { kind: "chat", threadId: decodeURIComponent(match[1]) };
  } catch {
    return { kind: "not-found" };
  }
}

export function chatPath(threadId: string): string {
  return `/chat/${encodeURIComponent(threadId)}`;
}
