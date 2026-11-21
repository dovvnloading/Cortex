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
  const [accepted, setAccepted] = useState(false);
  const [discarded, setDiscarded] = useState(false);
  const [retry, setRetry] = useState(0);
  const inFlight = useRef(false);

  useEffect(() => {
    let current = true;
    void api.changeset(runId).then((result) => {
      if (!current) return;
      setChanges(result.changes);
    }).catch((cause: unknown) => {
      if (!current) return;
      setLoadError(describeApiError(cause, "Could not load the staged changes. Try again."));
    });
    return () => { current = false; };
  }, [api, runId, retry]);

  const act = async (action: ReviewAction, paths: string[] = []): Promise<void> => {
    if (inFlight.current || discarded || changes === null
      || (action === "accept" && (accepted || paths.length === 0))
      || (action === "discard" && accepted)
      || (action === "undo" && !accepted)) return;

    inFlight.current = true;
    setBusy(action);
    setError(null);
    setNotice(null);
    try {
      if (action === "accept") {
        const result = await api.accept(runId, paths, true);
        // This is set only after the server acknowledges exactly the selected paths.
        setAccepted(true);
        setNotice("Accepted to the project: " + result.paths.join(", "));
      } else if (action === "discard") {
        await api.discard(runId, true);
        setDiscarded(true);
        setNotice("Work copy discarded. Discard did not alter the project.");
      } else {
        const result = await api.undo(runId, true);
        setAccepted(false);
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
      {changes === null && !loadError && <p role="status">Loading staged changes…</p>}
      {loadError && (
        <>
          <p role="alert" className="coding-error">{loadError}</p>
          <button type="button" className="coding-quiet" onClick={() => {
            setChanges(null);
            setLoadError(null);
            setError(null);
            setNotice(null);
            setRetry((value) => value + 1);
          }}>Retry changeset</button>
        </>
      )}
      {discarded ? <p role="status">{notice}</p> : changes !== null && (
        <ChangesReview
          summary="Review each proposed file change. Only your selected paths can be accepted, after a separate confirmation."
          changes={changes}
          accepted={accepted}
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
