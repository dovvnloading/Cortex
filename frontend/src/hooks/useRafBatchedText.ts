import { useCallback, useEffect, useRef, useState } from "react";

/** Coalesces many rapid push() calls into at most one state update per animation frame. */
export function useRafBatchedText(initial = "") {
  const [text, setText] = useState(initial);
  const bufferRef = useRef(initial);
  const rafRef = useRef<number | null>(null);

  const push = useCallback((chunk: string) => {
    bufferRef.current += chunk;
    if (rafRef.current == null) {
      rafRef.current = requestAnimationFrame(() => {
        setText(bufferRef.current);
        rafRef.current = null;
      });
    }
  }, []);

  const reset = useCallback((value = "") => {
    bufferRef.current = value;
    setText(value);
  }, []);

  useEffect(() => () => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
  }, []);

  return { text, push, reset };
}
