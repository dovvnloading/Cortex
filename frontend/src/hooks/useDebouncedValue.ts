import { useEffect, useState } from "react";

/**
 * `value`, but only once it has stopped changing for `delayMs`. For work that
 * is too costly to redo on every keystroke; the input itself should keep using
 * the live value so typing is never delayed.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
