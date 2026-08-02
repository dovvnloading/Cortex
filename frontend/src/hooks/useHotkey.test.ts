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
});
