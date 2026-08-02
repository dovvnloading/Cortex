import { create } from "zustand";

export type ToastKind = "success" | "error" | "info";
export type Toast = { id: number; kind: ToastKind; message: string };

const TOAST_LIFETIME_MS = 4500;

interface UiStoreState {
  toasts: Toast[];
  commandPaletteOpen: boolean;
  shortcutsDialogOpen: boolean;
  notify: (message: string, kind?: ToastKind) => void;
  dismissToast: (id: number) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setShortcutsDialogOpen: (open: boolean) => void;
}

export const useUiStore = create<UiStoreState>((set) => ({
  toasts: [],
  commandPaletteOpen: false,
  shortcutsDialogOpen: false,
  notify: (message, kind = "info") => {
    const id = Date.now() + Math.random();
    set((state) => ({ toasts: [...state.toasts, { id, kind, message }] }));
    window.setTimeout(() => {
      set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }));
    }, TOAST_LIFETIME_MS);
  },
  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) })),
  setCommandPaletteOpen: (commandPaletteOpen) => set({ commandPaletteOpen }),
  setShortcutsDialogOpen: (shortcutsDialogOpen) => set({ shortcutsDialogOpen }),
}));

/** Thin wrapper preserving the pre-Zustand useToast() API so call sites don't change. */
export function useToast(): { notify: (message: string, kind?: ToastKind) => void } {
  const notify = useUiStore((state) => state.notify);
  return { notify };
}
