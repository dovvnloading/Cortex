import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import type { CortexApi } from "../../api/client";
import { describeApiError } from "../../api/client";
import type { WorkspaceCheckpointSummary } from "../../api/checkpointTypes";

type RecoveryState = "loading" | "loaded" | "unavailable";
type Action = "refresh" | "restore" | "discard";

export function SavedRecoveryPanel({ api }: { api: CortexApi }) {
  const [checkpoints, setCheckpoints] = useState<WorkspaceCheckpointSummary[]>([]);
  const [recoveryState, setRecoveryState] = useState<RecoveryState>("loading");
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const mounted = useRef(false);
  const requestNumber = useRef(0);
  const inFlight = useRef(false);

  const reload = useCallback(async () => {
    const request = ++requestNumber.current;
    try {
      const result = await api.workspaceCheckpoints();
      if (!Array.isArray(result.checkpoints)) throw new Error("Invalid recovery list");
      if (mounted.current && request === requestNumber.current) {
        setCheckpoints(result.checkpoints);
        setRecoveryState("loaded");
      }
    } catch {
      // A failed journal scan is never equivalent to an empty journal.
      if (mounted.current && request === requestNumber.current) {
        setRecoveryState("unavailable");
      }
    }
  }, [api]);

  useEffect(() => {
    mounted.current = true;
    void reload();
    return () => {
      mounted.current = false;
      requestNumber.current += 1;
    };
  }, [reload]);

  const refresh = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy("refresh");
    setError(null);
    try {
      await reload();
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(null);
    }
  };

  const change = async (action: "restore" | "discard", checkpoint: WorkspaceCheckpointSummary) => {
    if (inFlight.current) return;
    const question = action === "restore"
      ? "Restore original source files for " + checkpoint.root + "? Cortex will verify ALL current file hashes and refuse files changed by other programs. No source will be overwritten unless it matches a recorded version."
      : "Permanently delete the saved recovery copy for " + checkpoint.root + "? Your project edits will remain and this recovery checkpoint CANNOT be restored afterward.";
    if (!window.confirm(question)) return;

    inFlight.current = true;
    setBusy(action);
    setError(null);
    setNotice(null);
    try {
      if (action === "restore") {
        const result = await api.restoreWorkspaceCheckpoint(checkpoint.id);
        if (result.status !== "restored" || result.checkpoint_id !== checkpoint.id || !Array.isArray(result.paths)) {
          throw new Error("The server did not confirm the restoration.");
        }
        setNotice("Restored " + result.paths.length + " source files from the saved checkpoint. No model or test was run.");
      } else {
        const result = await api.discardWorkspaceCheckpoint(checkpoint.id);
        if (result.discarded !== true) throw new Error("The server did not confirm the discard.");
        setNotice("Saved recovery copy discarded. Source files were not changed.");
      }
      // Do not show a false empty list if the follow-up scan fails.
      await reload();
    } catch (cause) {
      setError(describeApiError(cause, "The saved recovery operation failed. Check Cortex and the project, then retry."));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <section className="coding-panel" aria-label="Saved recovery checkpoints">
      <div className="coding-row">
        <h3>Saved recovery checkpoints</h3>
        <button type="button" className="coding-quiet" disabled={busy !== null}
          onClick={() => void refresh()}>Refresh recovery list</button>
      </div>
      <p className="coding-muted">
        Previously applied Project workspace patches have durable recovery copies here, even after a restart.
        This list is available without starting an Agent run. New Agent runs have their own confirmed undo
        in the run review above. Original source bytes are stored in a private, OS-protected SQLite journal,
        not separately encrypted. Recovery never runs automatically.
      </p>
      {notice && <p role="status">{notice}</p>}
      {error && <p className="coding-error" role="alert">{error}</p>}
      {recoveryState === "loading"
        ? <p className="coding-muted">Checking for saved recovery checkpoints…</p>
        : recoveryState === "unavailable"
          ? <p className="coding-error" role="alert">Saved recovery copies could not be checked. Do not assume there are none. Use Refresh recovery list to retry.</p>
          : checkpoints.length === 0
            ? <p className="coding-muted">No saved recovery checkpoints.</p>
            : <div className="coding-recovery-list">
                {checkpoints.map((item) => (
                  <div className="coding-recovery-row" key={item.id}>
                    <div className="coding-recovery-info">
                      <strong>{item.root}</strong>
                      <p className="coding-muted">{item.file_count} files · {item.state === "pending" ? "Interrupted/uncertain write" : "Approved edits"} · {new Date(item.updated_at).toLocaleString()}</p>
                      <p className="coding-muted">{item.paths.join(", ")}</p>
                    </div>
                    <div className="coding-row">
                      <button className="coding-quiet" type="button" disabled={busy !== null}
                        onClick={() => void change("restore", item)}><RotateCcw aria-hidden="true" size={15} /> Restore originals</button>
                      <button className="coding-quiet" type="button" disabled={busy !== null}
                        onClick={() => void change("discard", item)}>Discard backup</button>
                    </div>
                  </div>
                ))}
              </div>}
    </section>
  );
}
