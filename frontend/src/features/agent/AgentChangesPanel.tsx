import { useEffect, useRef, useState } from "react";
import type { AgentChange } from "../../../../contracts/cortex-api";
import { describeApiError } from "../../api/client";
import type { AgentApi } from "./agentApi";
import { ChangesReview } from "./ChangesReview";

type ReviewAction = "accept" | "discard" | "undo";

export type AgentChangesPanelProps = {
  api: AgentApi;
  runId: string;
};

/** Server-owned review lifecycle. A callback alone is never a successful edit. */
export function AgentChangesPanel({ api, runId }: AgentChangesPanelProps) {
  const [changes, setChanges] = useState<AgentChange[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<ReviewAction | null>(null);
  const [undoAvailable, setUndoAvailable] = useState(false);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [discarded, setDiscarded] = useState(false);
  const [retry, setRetry] = useState(0);
  const [recoveryConfirm, setRecoveryConfirm] = useState(false);
  const [recoveredWithoutCopy, setRecoveredWithoutCopy] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    let current = true;
    void api.changeset(runId).then((result) => {
      if (!current) return;
      setChanges(result.changes);
      setLoadError(null);
    }).catch((cause: unknown) => {
      if (!current) return;
      setLoadError(describeApiError(cause, "Could not load the staged changes. Try again."));
    });
    // The ledger and recovery journal, not this component's mount history,
    // determine whether there are accepted files to restore.
    void api.get(runId).then((run) => {
      if (!current) return;
      setUndoAvailable(run.undo_available === true);
      setAvailabilityError(null);
    }).catch(() => {
      if (!current) return;
      setUndoAvailable(false);
      setAvailabilityError("Could not check recovery availability. Retry to check again.");
    });
    return () => { current = false; };
  }, [api, runId, retry]);

  const retryReview = () => {
    setChanges(null);
    setLoadError(null);
    setUndoAvailable(false);
    setAvailabilityError(null);
    setError(null);
    setNotice(null);
    setRecoveryConfirm(false);
    setRetry((value) => value + 1);
  };

  const act = async (action: ReviewAction, paths: string[] = []): Promise<void> => {
    const recoveringWithoutCopy = changes === null && loadError !== null;
    const recoveringEmptyChangeset = changes?.length === 0;
    if (inFlight.current || recoveredWithoutCopy
      || (action !== "undo" && (discarded || changes === null))
      || (action === "accept" && paths.length === 0)
      || (action === "undo" && !undoAvailable)) return;

    inFlight.current = true;
    setBusy(action);
    setError(null);
    setNotice(null);
    try {
      if (action === "accept") {
        const result = await api.accept(runId, paths, true);
        // This is set only after the server acknowledges exactly the selected paths.
        setNotice("Accepted to the project: " + result.paths.join(", "));
        // Re-read the remaining staged files and the backend checkpoint state.
        setChanges(null);
        setRetry((value) => value + 1);
      } else if (action === "discard") {
        await api.discard(runId, true);
        setDiscarded(true);
        setChanges(null);
        setNotice("Work copy discarded. Discard did not alter the project.");
        // A discarded copy does not remove the durable undo checkpoint.
        setRetry((value) => value + 1);
      } else {
        const result = await api.undo(runId, true);
        setUndoAvailable(false);
        if (discarded || recoveringWithoutCopy || recoveringEmptyChangeset) {
          setRecoveredWithoutCopy(true);
        } else {
          setChanges(null);
          setRetry((value) => value + 1);
        }
        setNotice("Restored original project files: " + result.paths.join(", "));
      }
    } catch (cause) {
      const fallback = action === "accept"
        ? "Could not confirm acceptance. No success has been reported; check the project before retrying."
        : action === "undo"
          ? "Could not confirm undo. Your project files may still contain accepted edits."
          : "Could not confirm discard. The work copy may still exist.";
      setError(describeApiError(cause, fallback));
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  };

  return (
    <section className="coding-panel" aria-label="Changes review">
      <h3>Review staged changes</h3>
      {changes === null && !loadError && !discarded && !recoveredWithoutCopy && (
        <p role="status">Loading staged changes…</p>
      )}
      {loadError && !discarded && !recoveredWithoutCopy && (
        <>
          <p role="alert" className="coding-error">{loadError}</p>
          <button type="button" className="coding-quiet" disabled={busy !== null}
            onClick={retryReview}>Retry changeset</button>
        </>
      )}
      {availabilityError && !recoveredWithoutCopy && (
        <>
          <p role="alert" className="coding-error">{availabilityError}</p>
          <button type="button" className="coding-quiet" disabled={busy !== null}
            onClick={retryReview}>Retry recovery status</button>
        </>
      )}
      {(discarded || loadError) && !recoveredWithoutCopy && (
        <>
          {discarded && notice && <p role="status">{notice}</p>}
          {undoAvailable && (
            <p className="coding-muted">Previously accepted edits can still have a recovery checkpoint even after the work copy is discarded. No restoration happens without a separate confirmation.</p>
          )}
          {error && <p role="alert" className="coding-error">{error}</p>}
          {busy && <p role="status">Waiting for the server to confirm the action…</p>}
          {undoAvailable && (recoveryConfirm ? (
            <div role="group" aria-label="Undo confirmation">
              <p>Restore previously accepted project files from this run? Unexpected file changes will be refused.</p>
              <button type="button" className="coding-quiet" disabled={busy !== null}
                onClick={() => setRecoveryConfirm(false)}>Cancel</button>
              <button type="button" className="coding-primary" disabled={busy !== null}
                onClick={() => { setRecoveryConfirm(false); void act("undo"); }}>Confirm undo</button>
            </div>
          ) : (
            <button type="button" className="coding-quiet" disabled={busy !== null}
              onClick={() => setRecoveryConfirm(true)}>Undo previously accepted changes</button>
          ))}
        </>
      )}
      {recoveredWithoutCopy && <p role="status">{notice}</p>}
      {!discarded && changes !== null && !recoveredWithoutCopy && (
        <ChangesReview
          summary="Review each proposed file change. Only your selected paths can be accepted, after a separate confirmation."
          changes={changes}
          accepted={false}
          allowRecovery={undoAvailable}
          busy={busy !== null}
          error={error}
          notice={notice}
          onAccept={(paths) => void act("accept", paths)}
          onDiscard={() => void act("discard")}
          onUndo={() => void act("undo")}
        />
      )}
    </section>
  );
}
