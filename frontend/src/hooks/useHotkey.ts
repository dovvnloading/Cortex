import { useEffect, useRef } from "react";

export function isEditableTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.isContentEditable;
}

/**
 * Registers a global single-key shortcut. Modifier combos (Ctrl/Cmd+key) fire
 * even while typing, matching how palette shortcuts behave elsewhere; plain
 * keys (e.g. "?") are suppressed while focus is in an editable field so they
 * don't hijack normal typing.
 *
 * Three things a shortcut must not do:
 *  - fire again for every auto-repeat while the key is held (the palette
 *    toggle flickered open and shut under a held Ctrl+K);
 *  - fire in the middle of an IME composition, where keys belong to the
 *    composition;
 *  - treat AltGr as Ctrl. Chromium reports AltGr as Ctrl+Alt, so on a layout
 *    that types characters with AltGr, Ctrl+K matched while the person was
 *    simply typing one. A modifier combo therefore requires Alt to be up.
 *
 * The handler is held in a ref, so a new inline function each render does not
 * remove and re-add the window listener.
 */
export function useHotkey(key: string, withModifier: boolean, handler: () => void): void {
  const handlerRef = useRef(handler);
  // Assigned in an effect rather than during render, as react-hooks/refs requires.
  useEffect(() => {
    handlerRef.current = handler;
  });

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing) return;
      if (event.key.toLowerCase() !== key.toLowerCase()) return;
      const modifierMatches = withModifier
        ? (event.ctrlKey || event.metaKey) && !event.altKey
        : !event.ctrlKey && !event.metaKey && !event.altKey;
      if (!modifierMatches) return;
      if (!withModifier && isEditableTarget(event.target)) return;
      event.preventDefault();
      handlerRef.current();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [key, withModifier]);
}
