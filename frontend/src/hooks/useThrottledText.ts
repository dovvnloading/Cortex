import { useEffect, useRef, useState } from "react";

/**
 * A copy of `text` that changes at most once per `intervalMs`, for feeding a
 * consumer that is expensive per change (here: parsing Markdown).
 *
 * The source keeps changing at animation-frame rate while a reply streams in.
 * Handing every one of those values to a Markdown parser re-parses the whole
 * partial answer up to sixty times a second, on the same machine that is
 * running the model. Trailing throttle: the first change after a quiet spell is
 * shown at once, later ones are folded into one update per interval, and the
 * latest value is always delivered when the source stops changing, so the
 * displayed text can only ever lag, never end up wrong.
 *
 * `enabled = false` returns `text` itself with no delay -- used when a stream
 * has finished, so the final text is exact the moment it is known.
 */
export function useThrottledText(text: string, intervalMs: number, enabled: boolean): string {
  const [shown, setShown] = useState(text);
  const lastShownAt = useRef(0);

  useEffect(() => {
    if (!enabled || text === shown) return undefined;
    const wait = Math.max(0, lastShownAt.current + intervalMs - Date.now());
    const timer = window.setTimeout(() => {
      lastShownAt.current = Date.now();
      setShown(text);
    }, wait);
    return () => window.clearTimeout(timer);
  }, [text, shown, intervalMs, enabled]);

  return enabled ? shown : text;
}
