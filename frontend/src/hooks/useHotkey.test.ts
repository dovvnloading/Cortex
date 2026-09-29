import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useHotkey } from "./useHotkey";

function dispatchKey(key: string, options: Partial<KeyboardEventInit> = {}, target: EventTarget = window) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options });
  target.dispatchEvent(event);
  return event;
}

describe("useHotkey", () => {
  it("fires a modifier combo (Ctrl/Cmd+key) globally", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("k", true, handler));

    dispatchKey("k", { ctrlKey: true });
    expect(handler).toHaveBeenCalledTimes(1);

    dispatchKey("k", { metaKey: true });
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it("does not fire a modifier combo without the modifier held", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("k", true, handler));

    dispatchKey("k");
    expect(handler).not.toHaveBeenCalled();
  });

  it("fires a plain key when focus is not in an editable element", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("?", false, handler));

    dispatchKey("?");
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("suppresses a plain key while typing in an input or textarea", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("?", false, handler));

    const input = document.createElement("input");
    document.body.appendChild(input);
    dispatchKey("?", {}, input);
    expect(handler).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it("removes its listener on unmount", () => {
    const handler = vi.fn();
    const { unmount } = renderHook(() => useHotkey("k", true, handler));
    unmount();

    dispatchKey("k", { ctrlKey: true });
    expect(handler).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat, so holding Ctrl+K does not flicker the palette", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("k", true, handler));

    dispatchKey("k", { ctrlKey: true });
    dispatchKey("k", { ctrlKey: true, repeat: true });
    dispatchKey("k", { ctrlKey: true, repeat: true });

    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not fire in the middle of an IME composition", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("k", true, handler));

    dispatchKey("k", { ctrlKey: true, isComposing: true });

    expect(handler).not.toHaveBeenCalled();
  });

  it("does not take AltGr (reported as Ctrl+Alt) for Ctrl", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("k", true, handler));

    // AltGr+K types a character on layouts that have one there.
    const altGr = dispatchKey("k", { ctrlKey: true, altKey: true });
    expect(handler).not.toHaveBeenCalled();
    expect(altGr.defaultPrevented).toBe(false);

    dispatchKey("k", { metaKey: true, altKey: true });
    expect(handler).not.toHaveBeenCalled();

    // Plain Ctrl+K, and Cmd+K, still work.
    dispatchKey("k", { ctrlKey: true });
    dispatchKey("k", { metaKey: true });
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it("ignores a plain key while Alt is held", () => {
    const handler = vi.fn();
    renderHook(() => useHotkey("?", false, handler));

    dispatchKey("?", { altKey: true });

    expect(handler).not.toHaveBeenCalled();
  });

  it("keeps one listener however often the handler changes, and calls the latest handler", () => {
    const added: string[] = [];
    const removed: string[] = [];
    // Record the calls but still perform them, so the hook keeps working.
    const realAdd = window.addEventListener.bind(window);
    const realRemove = window.removeEventListener.bind(window);
    const trackAdd = vi.spyOn(window, "addEventListener").mockImplementation(((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
      added.push(type);
      realAdd(type, listener, options);
    }) as typeof window.addEventListener);
    const trackRemove = vi.spyOn(window, "removeEventListener").mockImplementation(((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | EventListenerOptions) => {
      removed.push(type);
      realRemove(type, listener, options);
    }) as typeof window.removeEventListener);
    try {
      const first = vi.fn();
      const second = vi.fn();
      const third = vi.fn();
      const { rerender } = renderHook(({ handler }) => useHotkey("k", true, handler), { initialProps: { handler: first } });
      const keydownAdds = () => added.filter((type) => type === "keydown").length;
      expect(keydownAdds()).toBe(1);

      // A fresh handler on every render used to remove and re-add the listener each time.
      rerender({ handler: second });
      rerender({ handler: third });
      expect(keydownAdds()).toBe(1);
      expect(removed.filter((type) => type === "keydown")).toHaveLength(0);

      dispatchKey("k", { ctrlKey: true });
      expect(third).toHaveBeenCalledTimes(1);
      expect(first).not.toHaveBeenCalled();
      expect(second).not.toHaveBeenCalled();
    } finally {
      trackAdd.mockRestore();
      trackRemove.mockRestore();
    }
  });

  it("re-registers when the key itself changes", () => {
    const handler = vi.fn();
    const { rerender } = renderHook(({ key }) => useHotkey(key, true, handler), { initialProps: { key: "k" } });

    rerender({ key: "j" });
    dispatchKey("k", { ctrlKey: true });
    expect(handler).not.toHaveBeenCalled();
    dispatchKey("j", { ctrlKey: true });
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
