import { create } from "zustand";

export type ToastKind = "success" | "error" | "info";
export type ToastAction = { label: string; onAction: () => void };
export type Toast = { id: number; kind: ToastKind; message: string; action?: ToastAction };

/** How long a success or info toast stays up. Errors stay until dismissed. */
const TOAST_LIFETIME_MS = 4500;
/** Errors accumulate now that they persist, so the stack is capped. */
const MAX_TOASTS = 5;

export type NotifyOptions = {
  /** A button on the toast, for an outcome the person can still reverse. */
  action?: ToastAction;
  /**
   * How long the toast stays up. Defaults to a few seconds for success and
   * info toasts, and to "until dismissed" for errors: a failure reason is
   * sometimes the only place the reason appears, so it must not vanish while
   * it is still being read.
   */
  durationMs?: number;
  /**
   * Called once when the toast goes away without its action having been used
   * (it timed out, was dismissed, or was pushed off the stack). This is what
   * lets a deferred change, such as a delete that can be undone, commit at
   * exactly the moment the person can no longer take it back.
   */
  onClose?: () => void;
};

interface UiStoreState {
  toasts: Toast[];
  commandPaletteOpen: boolean;
  shortcutsDialogOpen: boolean;
  /** Returns the toast's id. An identical error that is already showing is not stacked again. */
  notify: (message: string, kind?: ToastKind, options?: NotifyOptions) => number;
  dismissToast: (id: number) => void;
  /** Stop a toast's countdown while it is being read or used. No effect on toasts that do not expire. */
  pauseToast: (id: number) => void;
  resumeToast: (id: number) => void;
  /** Run a toast's action and remove it. The action replaces `onClose`, which is not called. */
  runToastAction: (id: number) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setShortcutsDialogOpen: (open: boolean) => void;
}

type ToastTimer = {
  handle: number | null;
  remainingMs: number;
  startedAt: number;
};

// Timers live beside the store rather than in it: they are not state anything
// renders, and each one must be cleared when its toast leaves.
const toastTimers = new Map<number, ToastTimer>();
const toastCloseHandlers = new Map<number, () => void>();
let nextToastId = 1;

function clearToastTimer(id: number): void {
  const timer = toastTimers.get(id);
  if (timer?.handle != null) window.clearTimeout(timer.handle);
  toastTimers.delete(id);
}

function takeCloseHandler(id: number): (() => void) | undefined {
  const handler = toastCloseHandlers.get(id);
  toastCloseHandlers.delete(id);
  return handler;
}

export const useUiStore = create<UiStoreState>((set, get) => {
  /** Remove a toast and, unless its action ran, tell its owner it is gone. */
  const closeToast = (id: number, runCloseHandler: boolean): void => {
    clearToastTimer(id);
    const onClose = takeCloseHandler(id);
    if (!get().toasts.some((toast) => toast.id === id)) return;
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }));
    if (runCloseHandler) onClose?.();
  };

  const armToastTimer = (id: number, remainingMs: number): void => {
    const handle = window.setTimeout(() => closeToast(id, true), remainingMs);
    toastTimers.set(id, { handle, remainingMs, startedAt: Date.now() });
  };

  return {
    toasts: [],
    commandPaletteOpen: false,
    shortcutsDialogOpen: false,
    notify: (message, kind = "info", options = {}) => {
      const { action, onClose } = options;
      if (kind === "error" && !action) {
        const duplicate = get().toasts.find((toast) => toast.kind === "error" && !toast.action && toast.message === message);
        if (duplicate) return duplicate.id;
      }
      const id = nextToastId++;
      // Past the cap the oldest toast makes room. It counts as closed, so a
      // pending undo commits rather than being left with no way to be used.
      const overflow = get().toasts.length + 1 - MAX_TOASTS;
      for (const evicted of get().toasts.slice(0, Math.max(0, overflow))) closeToast(evicted.id, true);
      if (onClose) toastCloseHandlers.set(id, onClose);
      set((state) => ({ toasts: [...state.toasts, action ? { id, kind, message, action } : { id, kind, message }] }));
      const lifetime = options.durationMs ?? (kind === "error" ? null : TOAST_LIFETIME_MS);
      if (lifetime !== null) armToastTimer(id, lifetime);
      return id;
    },
    dismissToast: (id) => closeToast(id, true),
    pauseToast: (id) => {
      const timer = toastTimers.get(id);
      if (!timer || timer.handle === null) return;
      window.clearTimeout(timer.handle);
      timer.remainingMs = Math.max(0, timer.remainingMs - (Date.now() - timer.startedAt));
      timer.handle = null;
    },
    resumeToast: (id) => {
      const timer = toastTimers.get(id);
      if (!timer || timer.handle !== null) return;
      armToastTimer(id, timer.remainingMs);
    },
    runToastAction: (id) => {
      const action = get().toasts.find((toast) => toast.id === id)?.action;
      closeToast(id, false);
      action?.onAction();
    },
    setCommandPaletteOpen: (commandPaletteOpen) => set({ commandPaletteOpen }),
    setShortcutsDialogOpen: (shortcutsDialogOpen) => set({ shortcutsDialogOpen }),
  };
});

/** Thin wrapper preserving the pre-Zustand useToast() API so call sites don't change. */
export function useToast(): { notify: (message: string, kind?: ToastKind, options?: NotifyOptions) => number } {
  const notify = useUiStore((state) => state.notify);
  return { notify };
}
