import { useEffect, useRef } from "react";

/**
 * Poll `callback` on an interval, but only while the window is actually being
 * looked at.
 *
 * Cortex is a desktop app that also runs a local model. There is no reason to
 * keep asking the backend questions every second while its window is minimised
 * or behind something else, so ticks are skipped when the document is hidden
 * and one immediate refresh fires on return rather than leaving the UI stale
 * until the next tick.
 *
 * `enabled` is the other half: a poll that has nothing to watch should not run
 * at all. Passing false stops the interval and clears the listener.
 *
 * The callback is held in a ref, so an inline arrow function does not restart
 * the interval on every render. Only `enabled` and `intervalMs` do that.
 */
export function useVisiblePolling(
  callback: () => void | Promise<unknown>,
  intervalMs: number,
  enabled: boolean,
): void {
  const callbackRef = useRef(callback);
  // Assigned in an effect rather than during render: a ref write in the render
  // body is exactly what react-hooks/refs forbids, and StrictMode runs the
  // render body twice.
  useEffect(() => {
    callbackRef.current = callback;
  });

  useEffect(() => {
    if (!enabled) return undefined;

    const run = () => {
      void callbackRef.current();
    };

    run();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") run();
    }, intervalMs);
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") run();
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [enabled, intervalMs]);
}
