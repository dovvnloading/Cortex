import { useCallback, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { AlertDialog, DialogContent } from "../../shared/ui/Dialog";

/** One entry from the workcopy changeset, with its path supplied by the caller. */
export type FileChange = {
  path: string;
  kind: "add" | "modify" | "delete";
  diff: string;
};

export type ChangesReviewProps = {
  summary: string;
  changes: readonly FileChange[];
  onAccept: (paths: string[]) => void;
  onDiscard: () => void;
  onUndo: () => void;
  accepted: boolean;
  busy?: boolean;
  error?: string | null;
  notice?: string | null;
};

type Confirmation = {
  action: "accept" | "discard" | "undo";
  signature: string;
  paths: string[];
};

const diffStyle = {
  margin: "12px 0 0",
  padding: 12,
  border: "1px solid var(--line)",
  borderRadius: "var(--radius-sm)",
  background: "var(--surface)",
  maxHeight: 240,
  overflow: "auto",
  overflowWrap: "anywhere",
  whiteSpace: "pre-wrap",
} as const;

const kindAppearance = {
  add: { className: "status-pill status-success", color: "var(--success)", background: "var(--success-soft)" },
  modify: { className: "status-pill", color: "var(--text-muted)", background: "var(--surface-soft)" },
  delete: { className: "status-pill status-danger", color: "var(--danger)", background: "var(--danger-soft)" },
} as const;

/** Keep unified diff headers neutral; only added/removed content gets colored. */
function diffLineKind(line: string): "added" | "removed" | "context" {
  if (line.startsWith("+") && !line.startsWith("+++ ")) return "added";
  if (line.startsWith("-") && !line.startsWith("--- ")) return "removed";
  return "context";
}

function DiffText({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <code>
      {lines.map((line, index) => {
        const kind = diffLineKind(line);
        return (
          <span
            key={index}
            data-diff-line={kind}
            style={{ color: kind === "added" ? "var(--success)" : kind === "removed" ? "var(--danger)" : undefined }}
          >
            {line}{index < lines.length - 1 ? "\n" : null}
          </span>
        );
      })}
    </code>
  );
}

/**
 * Displays a work-copy snapshot; every write remains with the caller, after
 * a deliberate selection and a second confirmation in this dialog.
 */
export function ChangesReview({
  summary, changes, onAccept, onDiscard, onUndo, accepted, busy = false, error, notice,
}: ChangesReviewProps) {
  const diffId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstCheckboxRef = useRef<HTMLInputElement>(null);
  const acceptButtonRef = useRef<HTMLButtonElement>(null);
  const discardButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const undoButtonRef = useRef<HTMLButtonElement>(null);
  const lastConfirmation = useRef<Confirmation["action"] | null>(null);
  const previouslyAccepted = useRef(accepted);

  // The signature keeps a confirmation tied to the exact displayed changes,
  // rather than accidentally approving a later workcopy snapshot.
  const signature = JSON.stringify(changes);
  const reviewKey = JSON.stringify([summary, signature, accepted]);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ signature: string; unchecked: string[] }>({
    signature,
    unchecked: [],
  });
  const [folds, setFolds] = useState<{ signature: string; expanded: string[] }>({
    signature,
    expanded: [],
  });
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  // New changesets start selected and folded; an unchanged prop rerender
  // preserves the user's selections and open diffs.
  const unchecked = selection.signature === signature ? selection.unchecked : [];
  const expanded = folds.signature === signature ? folds.expanded : [];
  const selectedPaths = changes.filter((change) => !unchecked.includes(change.path)).map((change) => change.path);
  const confirmationCurrent = confirmation !== null
    && confirmation.signature === signature
    && (confirmation.action !== "accept" || (
      selectedPaths.length > 0
      && selectedPaths.length === confirmation.paths.length
      && selectedPaths.every((path, index) => path === confirmation.paths[index])
    ));

  const changeSelection = (path: string, checked: boolean) => {
    setSelection((previous) => {
      const current = previous.signature === signature ? previous.unchecked : [];
      return {
        signature,
        unchecked: checked ? current.filter((item) => item !== path) : [...current.filter((item) => item !== path), path],
      };
    });
  };

  const toggleDiff = (path: string) => {
    setFolds((previous) => {
      const current = previous.signature === signature ? previous.expanded : [];
      return {
        signature,
        expanded: current.includes(path) ? current.filter((item) => item !== path) : [...current, path],
      };
    });
  };

  // ApprovalCard's focus pattern: focus the new confirmation, return to its
  // opener on cancellation, and focus Undo when the parent reports acceptance.
  useLayoutEffect(() => {
    if (accepted && !previouslyAccepted.current) {
      undoButtonRef.current?.focus();
    } else if (!accepted && previouslyAccepted.current) {
      (firstCheckboxRef.current ?? discardButtonRef.current)?.focus();
    } else if (confirmation) {
      (confirmationCurrent ? confirmButtonRef : cancelButtonRef).current?.focus();
    } else if (lastConfirmation.current) {
      (lastConfirmation.current === "accept" ? acceptButtonRef
        : lastConfirmation.current === "undo" ? undoButtonRef : discardButtonRef).current?.focus();
    }
    lastConfirmation.current = confirmation?.action ?? null;
    previouslyAccepted.current = accepted;
  }, [accepted, confirmation, confirmationCurrent]);

  const initialFocus = useCallback(() => {
    const active = document.activeElement;
    return active instanceof HTMLElement && dialogRef.current?.contains(active)
      ? active
      : (accepted ? undoButtonRef.current : firstCheckboxRef.current ?? discardButtonRef.current);
  }, [accepted]);

  // Match ApprovalCard's explicit Tab boundary wrapping in addition to Base UI.
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

  const completeConfirmation = () => {
    if (!confirmation || !confirmationCurrent || busy) return;
    if (confirmation.action === "accept" && !accepted && confirmation.paths.length > 0) {
      setConfirmation(null);
      onAccept([...confirmation.paths]);
    } else if (confirmation.action === "discard" && !accepted) {
      setConfirmation(null);
      onDiscard();
    } else if (confirmation.action === "undo" && accepted) {
      setConfirmation(null);
      onUndo();
    }
  };

  return (
    <>
      {dismissedFor === reviewKey && (
        <button type="button" className="button button-secondary" onClick={() => setDismissedFor(null)}>
          Reopen changes review
        </button>
      )}
    <AlertDialog.Root
      open={dismissedFor !== reviewKey}
      onOpenChange={(open) => {
        if (!open) {
          if (busy) return;
          if (confirmation) setConfirmation(null);
          else setDismissedFor(reviewKey);
        }
      }}
    >
      <DialogContent
        ref={dialogRef}
        style={{ width: "min(calc(100vw - 40px), 760px)" }}
        initialFocus={initialFocus}
        finalFocus={true}
        onKeyDownCapture={(event) => {
          if (event.key === "Escape" && confirmation) {
            event.preventDefault();
            event.stopPropagation();
            setConfirmation(null);
            return;
          }
          trapTab(event);
        }}
      >
        <AlertDialog.Title>Review agent changes</AlertDialog.Title>
        <AlertDialog.Description
          className="delete-dialog-description"
          style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
        >
          {summary}
        </AlertDialog.Description>
        {busy && <p role="status">Waiting for the server to confirm the action…</p>}
        {error && <p role="alert" className="coding-error">{error}</p>}
        {notice && <p role="status">{notice}</p>}

        {changes.length === 0 ? (
          <p>No files to review.</p>
        ) : (
          <ul aria-label="Changed files" style={{ listStyle: "none", padding: 0, margin: "16px 0", display: "grid", gap: 12 }}>
            {changes.map((change, index) => {
              const visible = expanded.includes(change.path);
              const id = diffId + "-" + index;
              const appearance = kindAppearance[change.kind];
              return (
                <li
                  key={change.path}
                  style={{ padding: 12, border: "1px solid var(--line)", borderRadius: "var(--radius-sm)" }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    <label style={{ display: "inline-flex", alignItems: "center", gap: 8, overflowWrap: "anywhere", flex: "1 1 220px" }}>
                      <input
                        ref={index === 0 ? firstCheckboxRef : undefined}
                        type="checkbox"
                        aria-label={"Select " + change.path}
                        checked={!unchecked.includes(change.path)}
                        disabled={accepted || confirmation !== null || busy}
                        onChange={(event) => changeSelection(change.path, event.target.checked)}
                      />
                      <span>{change.path}</span>
                    </label>
                    <span
                      className={appearance.className}
                      style={{ padding: "1px 8px", borderRadius: 999, color: appearance.color, background: appearance.background }}
                    >
                      {change.kind}
                    </span>
                    <button
                      type="button"
                      className="button button-secondary"
                      aria-expanded={visible}
                      aria-controls={visible ? id : undefined}
                      disabled={confirmation !== null || busy}
                      onClick={() => toggleDiff(change.path)}
                    >
                      {visible ? "Hide changes" : "Show changes"}
                    </button>
                  </div>
                  {visible && <pre id={id} style={diffStyle}><DiffText diff={change.diff} /></pre>}
                </li>
              );
            })}
          </ul>
        )}

        {confirmation ? (
          <div role="group" aria-label={confirmation.action === "accept" ? "Accept confirmation"
            : confirmation.action === "undo" ? "Undo confirmation" : "Discard confirmation"}>
            <p>
              {confirmation.action === "accept"
                ? "Apply changes to " + confirmation.paths.length + " " + (confirmation.paths.length === 1 ? "file" : "files") + " in your project?"
                : confirmation.action === "undo"
                  ? "Restore the original project files from this run? Undo will be refused if any file has unexpected changes."
                  : "Discard all " + changes.length + " pending " + (changes.length === 1 ? "file change" : "file changes") + "? No project files will be updated."}
            </p>
            {!confirmationCurrent && <p role="alert">Changes or selection changed. Cancel and review again.</p>}
            <div className="dialog-actions" style={{ marginTop: 16 }}>
              <button ref={cancelButtonRef} type="button" className="button button-secondary" onClick={() => setConfirmation(null)}>
                Cancel
              </button>
              <button
                ref={confirmButtonRef}
                type="button"
                className={confirmation.action === "discard" ? "button button-danger" : "button button-primary"}
                disabled={!confirmationCurrent || busy}
                onClick={completeConfirmation}
              >
                {confirmation.action === "accept" ? "Confirm accept"
                  : confirmation.action === "undo" ? "Confirm undo" : "Confirm discard"}
              </button>
            </div>
          </div>
        ) : accepted ? (
          <div className="dialog-actions" style={{ marginTop: 20 }}>
            <span role="status" style={{ marginRight: "auto" }}>Changes accepted to the project.</span>
            <button ref={undoButtonRef} type="button" className="button button-secondary"
              disabled={busy}
              onClick={() => setConfirmation({ action: "undo", signature, paths: [] })}>
              Undo
            </button>
          </div>
        ) : (
          <div className="dialog-actions" style={{ marginTop: 20, flexWrap: "wrap" }}>
            <button
              ref={discardButtonRef}
              type="button"
              className="button button-secondary"
              disabled={busy}
              onClick={() => setConfirmation({ action: "discard", signature, paths: [] })}
            >
              Discard all
            </button>
            <button
              ref={acceptButtonRef}
              type="button"
              className="button button-primary"
              disabled={selectedPaths.length === 0 || busy}
              onClick={() => {
                if (selectedPaths.length > 0) setConfirmation({ action: "accept", signature, paths: [...selectedPaths] });
              }}
            >
              Accept selected to project
            </button>
          </div>
        )}
      </DialogContent>
    </AlertDialog.Root>
    </>
  );
}
