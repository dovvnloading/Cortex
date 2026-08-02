/* eslint-disable react-refresh/only-export-components */
import { Check, Info, X } from "lucide-react";
import type { ReactNode } from "react";
import { useUiStore } from "../stores/useUiStore";

export { useToast } from "../stores/useUiStore";

export function ToastProvider({ children }: { children: ReactNode }) {
  const toasts = useUiStore((state) => state.toasts);

  return (
    <>
      {children}
      <div className="toast-region" aria-live="polite" aria-atomic="true">
        {toasts.map((toast) => (
          <div className={`toast toast-${toast.kind}`} key={toast.id} role="status">
            {toast.kind === "success" && <Check aria-hidden="true" size={16} />}
            {toast.kind === "error" && <X aria-hidden="true" size={16} />}
            {toast.kind === "info" && <Info aria-hidden="true" size={16} />}
            <span>{toast.message}</span>
          </div>
        ))}
      </div>
    </>
  );
}
