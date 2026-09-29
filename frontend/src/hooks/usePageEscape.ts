import { useEffect, useRef } from "react";
import { isEditableTarget } from "./useHotkey";

/**
 * The overlays that own Escape while they are open: dialogs (the shortcuts
 * reference, confirmations, the command palette), the popovers that are marked
 * up as dialogs, and open menus and listboxes. Each closes on Escape, and that
 * one keypress must not also do something to the page underneath.
 */
const OVERLAY_SELECTOR = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

/**
 * Runs `onEscape` when Escape is pressed on the page itself while `enabled`.
 *
 * It is for an action that is otherwise reachable only from one focused
 * control (stopping a response from the composer). Escape is left alone when
 * something else has a claim on it:
 *  - an element already handled it (`defaultPrevented`), which is how the
 *    composer's own Escape handling keeps this from running twice;
 *  - an overlay is open, whose Escape closes it;
 *  - focus is in some other text field, where Escape cancels the edit;
 *  - it is an auto-repeat, an IME composition, or carries a modifier.
 *
 * The handler is held in a ref, so a new function each render does not remove
 * and re-add the listener.
 */
export function usePageEscape(onEscape: () => void, enabled: boolean): void {
  const handlerRef = useRef(onEscape);
  // Assigned in an effect rather than during render, as react-hooks/refs requires.
  useEffect(() => {
    handlerRef.current = onEscape;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    const listener = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (event.repeat || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (isEditableTarget(event.target)) return;
      if (document.querySelector(OVERLAY_SELECTOR)) return;
      event.preventDefault();
      handlerRef.current();
    };
    // On the document, which the React handlers below the root have already
    // run before, so an element's own handling (and its preventDefault) is seen.
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [enabled]);
}
