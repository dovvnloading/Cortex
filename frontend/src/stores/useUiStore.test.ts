import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useUiStore } from "./useUiStore";

describe("useUiStore", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useUiStore.setState({ toasts: [], commandPaletteOpen: false, shortcutsDialogOpen: false });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("notify appends a toast and auto-dismisses it after its lifetime", () => {
    useUiStore.getState().notify("Chat renamed.", "success");
    expect(useUiStore.getState().toasts).toMatchObject([{ kind: "success", message: "Chat renamed." }]);

    vi.advanceTimersByTime(4500);
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("notify defaults to info and dismissToast removes a toast immediately", () => {
    useUiStore.getState().notify("Reconnecting...");
    const [toast] = useUiStore.getState().toasts;
    expect(toast.kind).toBe("info");

    useUiStore.getState().dismissToast(toast.id);
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("command palette and shortcuts dialog visibility toggle independently", () => {
    useUiStore.getState().setCommandPaletteOpen(true);
    expect(useUiStore.getState().commandPaletteOpen).toBe(true);
    expect(useUiStore.getState().shortcutsDialogOpen).toBe(false);
  });
});
