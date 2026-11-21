import { useId, useState } from "react";
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
  const [visibleCommandFor, setVisibleCommandFor] = useState<string | null>(null);
  const [reviewingDangerousFor, setReviewingDangerousFor] = useState<string | null>(null);
  const [acknowledgedDangerousFor, setAcknowledgedDangerousFor] = useState<string | null>(null);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);

  // A changed request must not inherit a prior command reveal or confirmation.
  const requestKey = JSON.stringify([
    request.command, request.cls, request.plain_text, request.sandbox_level, request.network,
  ]);
  const dangerous = request.cls === "dangerous";
  const commandVisible = visibleCommandFor === requestKey;
  const reviewingDangerous = reviewingDangerousFor === requestKey;
  const dangerousConfirmed = reviewingDangerous && acknowledgedDangerousFor === requestKey;
  const canAlwaysAllow = request.sandbox_level === "appcontainer"
    && !request.network
    && (request.cls === "inspect" || request.cls === "build_test");

  return (
    <AlertDialog.Root
      open={dismissedFor !== requestKey}
      // Escape or dismissal closes locally, without granting or denying anything.
      // Only a deliberate action-button click invokes one of the caller's callbacks.
      onOpenChange={(open) => { if (!open) setDismissedFor(requestKey); }}
    >
      <DialogContent>
        <AlertDialog.Title>Allow this command?</AlertDialog.Title>
        <AlertDialog.Description className="delete-dialog-description">
          {request.plain_text}
        </AlertDialog.Description>

        <div role="group" aria-label="Execution context" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <span className={`status-pill ${request.sandbox_level === "appcontainer" ? "status-success" : "status-danger"}`}>
            {request.sandbox_level === "appcontainer" ? "Sandboxed" : "Runs as you"}
          </span>
          {request.network && <span className="status-pill status-danger">Uses the internet</span>}
        </div>

        <div style={{ marginTop: 16 }}>
          <button
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
                <label className="field-label" style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12 }}>
                  <input
                    type="checkbox"
                    autoFocus
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
