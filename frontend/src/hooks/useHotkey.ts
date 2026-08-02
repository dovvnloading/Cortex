import { useEffect } from "react";

function isEditableTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.isContentEditable;
}

/**
 * Registers a global single-key shortcut. Modifier combos (Ctrl/Cmd+key) fire
 * even while typing, matching how palette shortcuts behave elsewhere; plain
 * keys (e.g. "?") are suppressed while focus is in an editable field so they
 * don't hijack normal typing.
 */
export function useHotkey(key: string, withModifier: boolean, handler: () => void): void {
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== key.toLowerCase()) return;
      const modifierMatches = withModifier
        ? event.ctrlKey || event.metaKey
        : !event.ctrlKey && !event.metaKey && !event.altKey;
      if (!modifierMatches) return;
      if (!withModifier && isEditableTarget(event.target)) return;
      event.preventDefault();
      handler();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [key, withModifier, handler]);
}
