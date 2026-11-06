import { useEffect, useRef } from "react";

export type VisiblePollingOptions = {
  /**
   * Slow down while `callback` keeps failing. Once it has failed three times in
   * a row (it throws, or its promise rejects), each further failure doubles the
   * wait, up to thirty seconds; one success returns to the normal interval. A
   * backend that is down or restarting is not asked every second, and the
   * return of the window to the foreground still checks at once.
   */
  backoff?: boolean;
};

/** Failures in a row that are tolerated before the wait starts to grow. */
const BACKOFF_AFTER_FAILURES = 3;
const MAX_BACKOFF_MS = 30_000;

/**
 * How long to wait before the next tick, given the base interval and how many
 * polls in a row have failed. Never shorter than the base interval, even when
 * that is already longer than the cap.
 */
export function pollDelay(intervalMs: number, consecutiveFailures: number): number {
  if (consecutiveFailures < BACKOFF_AFTER_FAILURES) return intervalMs;
  const doublings = consecutiveFailures - BACKOFF_AFTER_FAILURES + 1;
  return Math.min(intervalMs * 2 ** doublings, Math.max(MAX_BACKOFF_MS, intervalMs));
}

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return typeof value === "object" && value !== null && typeof (value as { then?: unknown }).then === "function";
}

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
 * at all. Passing false stops the timer and clears the listener, and turning it
 * on runs the callback once straight away.
 *
 * Changing `intervalMs` only re-arms the timer with the new period. It does not
 * run the callback: callers switch the interval on the very result a poll just
 * delivered (fast while something is running, slow when idle), and an extra
 * request per switch would answer a question that was just answered.
 *
 * The callback is held in a ref, so an inline arrow function does not restart
 * the timer on every render. A callback that throws or rejects counts as a
 * failed poll (see `backoff`); it is never left as an unhandled rejection.
 */
export function useVisiblePolling(
  callback: () => void | Promise<unknown>,
  intervalMs: number,
  enabled: boolean,
  { backoff = false }: VisiblePollingOptions = {},
): void {
  const callbackRef = useRef(callback);
  // Assigned in an effect rather than during render: a ref write in the render
  // body is exactly what react-hooks/refs forbids, and StrictMode runs the
  // render body twice.
  useEffect(() => {
    callbackRef.current = callback;
  });

  // Whether the previous run of the effect below had polling on. It is how the
  // effect tells "just turned on" (poll now) from "the interval changed" (do not).
  const wasEnabledRef = useRef(false);
  const failuresRef = useRef(0);

  useEffect(() => {
    const justEnabled = enabled && !wasEnabledRef.current;
    wasEnabledRef.current = enabled;
    if (!enabled) {
      failuresRef.current = 0;
      return undefined;
    }
    if (justEnabled) failuresRef.current = 0;

    let stopped = false;
    let timer: number | undefined;
    let armedDelay = intervalMs;

    const delay = () => (backoff ? pollDelay(intervalMs, failuresRef.current) : intervalMs);
    // The timer, not the request, sets the cadence: a poll that never answers
    // cannot stop the ones after it.
    const tick = () => {
      if (document.visibilityState === "visible") run();
    };
    const arm = () => {
      armedDelay = delay();
      timer = window.setInterval(tick, armedDelay);
    };
    // A finished poll can change how long to wait (the third failure in a row,
    // or the first success after some). The running timer has the old period,
    // so it is replaced rather than left to fire early or late.
    const settled = (failed: boolean) => {
      if (stopped || !backoff) return;
      failuresRef.current = failed ? failuresRef.current + 1 : 0;
      if (delay() !== armedDelay) {
        window.clearInterval(timer);
        arm();
      }
    };
    const run = () => {
      let result: void | Promise<unknown>;
      try {
        result = callbackRef.current();
      } catch {
        settled(true);
        return;
      }
      if (isPromiseLike(result)) {
        void Promise.resolve(result).then(() => settled(false), () => settled(true));
      } else {
        settled(false);
      }
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") run();
    };

    // Armed before the first run: a finished run may need to replace the timer.
    arm();
    if (justEnabled) run();
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [enabled, intervalMs, backoff]);
}
