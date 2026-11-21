import { useCallback, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { AlertDialog, DialogContent } from "../../shared/ui/Dialog";

/** The command categories and sandbox levels reported by agent/policy.py. */
export type ApprovalRequest = {
  command: string;
  cls: "inspect" | "build_test" | "mutate_workcopy" | "network" | "dangerous" | "ask";
  plain_text: string;
  sandbox_level: "appcontainer" | "unsandboxed";
  network: boolean;
};

export type ApprovalCardProps = {
  request: ApprovalRequest;
  onAllowOnce: () => void;
  onAlwaysAllow: () => void;
  onDeny: () => void;
};

const commandStyle = {
  margin: "8px 0 0",
  padding: 12,
  border: "1px solid var(--line)",
  borderRadius: "var(--radius-sm)",
  background: "var(--surface)",
  maxHeight: 180,
  overflowY: "auto",
  overflowWrap: "anywhere",
  whiteSpace: "pre-wrap",
} as const;

/**
 * Future Agent mode's approval surface. This component only asks for consent;
 * it never runs a command or persists a project rule.
 */
export function ApprovalCard({ request, onAllowOnce, onAlwaysAllow, onDeny }: ApprovalCardProps) {
  const commandId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const commandToggleRef = useRef<HTMLButtonElement>(null);
  const dangerousAcknowledgmentRef = useRef<HTMLInputElement>(null);
  const [visibleCommandFor, setVisibleCommandFor] = useState<ApprovalRequest | null>(null);
  const [reviewingDangerousFor, setReviewingDangerousFor] = useState<ApprovalRequest | null>(null);
  const [acknowledgedDangerousFor, setAcknowledgedDangerousFor] = useState<ApprovalRequest | null>(null);
  const [dismissedFor, setDismissedFor] = useState<ApprovalRequest | null>(null);

  // Bind review, acknowledgment, and dismissal to this request object.
  // A later request with identical text must still obtain fresh consent.
  const requestKey = request;
  const dangerous = request.cls === "dangerous";
  const commandVisible = visibleCommandFor === requestKey;
  const reviewingDangerous = reviewingDangerousFor === requestKey;
  const dangerousConfirmed = reviewingDangerous && acknowledgedDangerousFor === requestKey;
  const canAlwaysAllow = request.sandbox_level === "appcontainer"
    && !request.network
    && (request.cls === "inspect" || request.cls === "build_test");

  // Move focus to the second confirmation after its checkbox is mounted.
  useLayoutEffect(() => {
    if (reviewingDangerous) dangerousAcknowledgmentRef.current?.focus();
  }, [reviewingDangerous]);

  // Preserve a keyboard user's explicit focus even when the dialog's
  // initial-focus effect runs after they have focused one of its buttons.
  const initialFocus = useCallback(() => {
    const active = document.activeElement;
    return active instanceof HTMLElement && dialogRef.current?.contains(active)
      ? active
      : commandToggleRef.current;
  }, []);

  // Explicitly wrap Tab at the boundaries so focus cannot escape the card.
  const trapTab = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab" || event.defaultPrevented) return;
    const tabbable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) ?? []).filter((element) => element.tabIndex >= 0 && !element.closest("[hidden], [inert]"));
    if (tabbable.length === 0) return;
    const activeIndex = tabbable.indexOf(document.activeElement as HTMLElement);
    if (event.shiftKey && activeIndex <= 0) {
      event.preventDefault();
      tabbable[tabbable.length - 1].focus();
    } else if (!event.shiftKey && (activeIndex === -1 || activeIndex === tabbable.length - 1)) {
      event.preventDefault();
      tabbable[0].focus();
    }
  };

  // Escape or dismissal closes locally without consent; Base UI restores focus.
  return (
    <AlertDialog.Root
      open={dismissedFor !== requestKey}
      onOpenChange={(open) => { if (!open) setDismissedFor(requestKey); }}
    >
      <DialogContent ref={dialogRef} initialFocus={initialFocus} finalFocus={true} onKeyDownCapture={trapTab}>
        <AlertDialog.Title>Allow this command?</AlertDialog.Title>
        <AlertDialog.Description className="delete-dialog-description">
          {request.plain_text}
        </AlertDialog.Description>

        <div role="group" aria-label="Execution context" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <span
            className={`status-pill ${request.sandbox_level === "appcontainer" ? "status-success" : "status-danger"}`}
            style={{
              padding: "0 8px",
              borderRadius: 999,
              background: request.sandbox_level === "appcontainer" ? "var(--success-soft)" : "var(--danger-soft)",
            }}
          >
            {request.sandbox_level === "appcontainer" ? "Sandboxed" : "Runs as you"}
          </span>
          {request.network && (
            <span className="status-pill status-danger" style={{ padding: "0 8px", borderRadius: 999, background: "var(--danger-soft)" }}>
              Uses the internet
            </span>
          )}
        </div>

        <div style={{ marginTop: 16 }}>
          <button
            ref={commandToggleRef}
            type="button"
            className="button button-secondary"
            aria-expanded={commandVisible}
            aria-controls={commandVisible ? commandId : undefined}
            onClick={() => setVisibleCommandFor(commandVisible ? null : requestKey)}
          >
            {commandVisible ? "Hide command" : "Show command"}
          </button>
          {commandVisible && <pre id={commandId} style={commandStyle}><code>{request.command}</code></pre>}
        </div>

        {dangerous && (
          <div style={{ marginTop: 16 }}>
            {reviewingDangerous ? (
              <div role="group" aria-label="Dangerous command second confirmation">
                <p>This may access private information or change system settings. Review the exact command before approving one run.</p>
                <pre style={commandStyle}><code>{request.command}</code></pre>
                <label className="coding-consent" style={{ marginTop: 12 }}>
                  <input
                    ref={dangerousAcknowledgmentRef}
                    type="checkbox"
                    checked={dangerousConfirmed}
                    onChange={(event) => setAcknowledgedDangerousFor(event.target.checked ? requestKey : null)}
                  />
                  I have reviewed this exact command and approve one run.
                </label>
              </div>
            ) : (
              <button
                type="button"
                className="button button-secondary"
                onClick={() => {
                  setAcknowledgedDangerousFor(null);
                  setReviewingDangerousFor(requestKey);
                }}
              >
                Review dangerous command
              </button>
            )}
          </div>
        )}

        <div className="dialog-actions" style={{ marginTop: 20, flexWrap: "wrap" }}>
          <button type="button" className="button button-secondary" onClick={onDeny}>Deny</button>
          {canAlwaysAllow && (
            <button type="button" className="button button-secondary" onClick={onAlwaysAllow}>
              Always allow in this project
            </button>
          )}
          <button
            type="button"
            className="button button-primary"
            disabled={dangerous && !dangerousConfirmed}
            onClick={() => {
              if (dangerous) setAcknowledgedDangerousFor(null);
              onAllowOnce();
            }}
          >
            Allow once
          </button>
        </div>
      </DialogContent>
    </AlertDialog.Root>
  );
}
