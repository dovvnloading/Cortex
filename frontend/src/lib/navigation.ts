import { useCallback, useSyncExternalStore } from "react";

const NAVIGATION_EVENT = "cortex:navigation";

export type AppRoute =
  | { kind: "chat"; threadId: string | null }
  | { kind: "settings" }
  | { kind: "not-found" };

export type NavigateOptions = {
  replace?: boolean;
};

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
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const next = `${destination.pathname}${destination.search}${destination.hash}`;
  if (current === next) return;

  window.history[options.replace ? "replaceState" : "pushState"]({}, "", next);
  window.dispatchEvent(new Event(NAVIGATION_EVENT));
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
