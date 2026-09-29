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

  it("keeps an error toast until it is dismissed", () => {
    // A failure reason is sometimes the only place it appears (a failed model
    // download points the form at the notification), so it must outlive the
    // time it takes to read a 240-character validation message.
    const id = useUiStore.getState().notify("Checksum did not match the published file.", "error");

    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(useUiStore.getState().toasts).toMatchObject([{ id, kind: "error" }]);

    useUiStore.getState().dismissToast(id);
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("lets a caller give an error a lifetime, and gives info toasts the default one", () => {
    useUiStore.getState().notify("Transient failure.", "error", { durationMs: 1000 });
    useUiStore.getState().notify("Working on it.", "info");

    vi.advanceTimersByTime(1000);
    expect(useUiStore.getState().toasts.map((toast) => toast.message)).toEqual(["Working on it."]);
    vi.advanceTimersByTime(3500);
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("does not stack an identical error while it is still showing", () => {
    const first = useUiStore.getState().notify("Could not rename chat.", "error");
    const again = useUiStore.getState().notify("Could not rename chat.", "error");
    const other = useUiStore.getState().notify("Could not delete chat.", "error");

    expect(again).toBe(first);
    expect(other).not.toBe(first);
    expect(useUiStore.getState().toasts.map((toast) => toast.message)).toEqual(["Could not rename chat.", "Could not delete chat."]);

    // Once dismissed, the same failure is worth showing again.
    useUiStore.getState().dismissToast(first);
    useUiStore.getState().notify("Could not rename chat.", "error");
    expect(useUiStore.getState().toasts.map((toast) => toast.message)).toEqual(["Could not delete chat.", "Could not rename chat."]);
  });

  it("holds a toast's countdown while it is paused and finishes the remainder on resume", () => {
    const id = useUiStore.getState().notify("Saved.", "success");

    vi.advanceTimersByTime(3000);
    useUiStore.getState().pauseToast(id);
    vi.advanceTimersByTime(60_000);
    expect(useUiStore.getState().toasts).toHaveLength(1);

    useUiStore.getState().resumeToast(id);
    vi.advanceTimersByTime(1499);
    expect(useUiStore.getState().toasts).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(useUiStore.getState().toasts).toEqual([]);
  });

  it("runs a toast's action, removes the toast, and does not report it as closed", () => {
    const onAction = vi.fn();
    const onClose = vi.fn();
    const id = useUiStore.getState().notify("Chat deleted.", "success", { action: { label: "Undo", onAction }, onClose });

    useUiStore.getState().runToastAction(id);
    expect(onAction).toHaveBeenCalledOnce();
    expect(useUiStore.getState().toasts).toEqual([]);

    // Its countdown was cleared with it, so nothing fires later.
    vi.advanceTimersByTime(60_000);
    expect(onClose).not.toHaveBeenCalled();
    useUiStore.getState().runToastAction(id);
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("reports a toast as closed exactly once when it times out or is dismissed", () => {
    const onExpire = vi.fn();
    useUiStore.getState().notify("Chat deleted.", "success", { durationMs: 6000, onClose: onExpire });
    vi.advanceTimersByTime(5999);
    expect(onExpire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onExpire).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(60_000);
    expect(onExpire).toHaveBeenCalledOnce();

    const onDismiss = vi.fn();
    const id = useUiStore.getState().notify("Chat deleted.", "success", { durationMs: 6000, onClose: onDismiss });
    useUiStore.getState().dismissToast(id);
    useUiStore.getState().dismissToast(id);
    vi.advanceTimersByTime(60_000);
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("caps the stack, closing the oldest toast to make room", () => {
    const onClose = vi.fn();
    useUiStore.getState().notify("First", "error", { onClose });
    for (const message of ["Second", "Third", "Fourth", "Fifth"]) useUiStore.getState().notify(message, "error");
    expect(useUiStore.getState().toasts).toHaveLength(5);
    expect(onClose).not.toHaveBeenCalled();

    useUiStore.getState().notify("Sixth", "error");
    expect(useUiStore.getState().toasts.map((toast) => toast.message)).toEqual(["Second", "Third", "Fourth", "Fifth", "Sixth"]);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("command palette and shortcuts dialog visibility toggle independently", () => {
    useUiStore.getState().setCommandPaletteOpen(true);
    expect(useUiStore.getState().commandPaletteOpen).toBe(true);
    expect(useUiStore.getState().shortcutsDialogOpen).toBe(false);
  });
});
