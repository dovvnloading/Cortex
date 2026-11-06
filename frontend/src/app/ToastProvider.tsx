/* eslint-disable react-refresh/only-export-components */
import { AlertTriangle, Check, Info, X } from "lucide-react";
import type { FocusEvent, ReactNode } from "react";
import { useUiStore, type Toast } from "../stores/useUiStore";

export { useToast } from "../stores/useUiStore";

export function ToastProvider({ children }: { children: ReactNode }) {
  const toasts = useUiStore((state) => state.toasts);

  return (
    <>
      {children}
      {/*
        Each toast is its own live region (an alert for a failure, a status for
        the rest), so the region itself announces nothing and a toast is never
        read twice.
      */}
      <div className="toast-region" data-keeps-escape="">
        {toasts.map((toast) => <ToastItem key={toast.id} toast={toast} />)}
      </div>
    </>
  );
}

function ToastItem({ toast }: { toast: Toast }) {
  const dismissToast = useUiStore((state) => state.dismissToast);
  const pauseToast = useUiStore((state) => state.pauseToast);
  const resumeToast = useUiStore((state) => state.resumeToast);
  const runToastAction = useUiStore((state) => state.runToastAction);
  const { id } = toast;
  const isError = toast.kind === "error";

  // A toast that is being hovered or has keyboard focus inside it is being
  // read or used, so its countdown waits.
  const resumeUnlessFocusStaysInside = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) resumeToast(id);
  };

  return (
    <div
      className={`toast toast-${toast.kind}`}
      role={isError ? "alert" : "status"}
      onMouseEnter={() => pauseToast(id)}
      onMouseLeave={() => resumeToast(id)}
      onFocus={() => pauseToast(id)}
      onBlur={resumeUnlessFocusStaysInside}
    >
      {toast.kind === "success" && <Check aria-hidden="true" size={16} />}
      {isError && <AlertTriangle aria-hidden="true" size={16} />}
      {toast.kind === "info" && <Info aria-hidden="true" size={16} />}
      <span className="toast-message">{toast.message}</span>
      {toast.action && (
        <button type="button" className="toast-action" onClick={() => runToastAction(id)}>
          {toast.action.label}
        </button>
      )}
      {isError && (
        <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={() => dismissToast(id)}>
          <X aria-hidden="true" size={14} />
        </button>
      )}
    </div>
  );
}
