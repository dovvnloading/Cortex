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

/**
 * Register a guard that can hold back `navigate()`, for example to protect
 * unsaved edits. Returns the function that removes it.
 *
 * This covers navigation the app initiates. The browser's own back and forward
 * buttons change the location before any script runs, so a guard cannot stop
 * them.
 */
export function registerNavigationGuard(guard: NavigationGuard): () => void {
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
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATION_EVENT, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATION_EVENT, listener);
  };
}

function currentPathname(): string {
  return window.location.pathname;
}

export function usePathname(): string {
  return useSyncExternalStore(subscribe, currentPathname, () => "/chat/new");
}

export function navigate(to: string, options: NavigateOptions = {}): void {
  if (!to.startsWith("/") || to.startsWith("//")) {
    throw new Error("Cortex navigation only accepts same-origin absolute paths.");
  }

  const destination = new URL(to, window.location.origin);
  if (destination.origin !== window.location.origin) {
    throw new Error("Cortex navigation only accepts same-origin absolute paths.");
  }
  const currentLocation = () => `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const next = `${destination.pathname}${destination.search}${destination.hash}`;
  if (currentLocation() === next) return;

  const commit = () => {
    if (currentLocation() === next) return;
    window.history[options.replace ? "replaceState" : "pushState"]({}, "", next);
    window.dispatchEvent(new Event(NAVIGATION_EVENT));
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
