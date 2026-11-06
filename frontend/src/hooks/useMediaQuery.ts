import { useCallback, useSyncExternalStore } from "react";

export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function mediaQueryList(query: string): MediaQueryList | undefined {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(query)
    : undefined;
}

/**
 * Whether a CSS media query currently matches, kept up to date as it changes.
 *
 * Where `matchMedia` does not exist (tests, some embedded webviews) nothing
 * matches, which for the queries this app asks -- "reduce motion", for one --
 * means the ordinary behaviour.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    const list = mediaQueryList(query);
    if (!list) return () => undefined;
    if (typeof list.addEventListener === "function") {
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    }
    // Older WebView builds only have the deprecated pair.
    list.addListener?.(onChange);
    return () => list.removeListener?.(onChange);
  }, [query]);
  return useSyncExternalStore(subscribe, () => mediaQueryList(query)?.matches ?? false, () => false);
}

/**
 * True when the person has asked their system to reduce motion. The stylesheet
 * already honours that for CSS animations and transitions; this is for motion
 * that script starts, such as smooth programmatic scrolling, which CSS cannot reach.
 */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION_QUERY);
}
