import { Eraser, Plus, RefreshCw, Trash2, Save, Undo2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { AlertDialog, DialogContent } from "../../shared/ui/Dialog";

/** Where the list came from. The store is read when Settings opens, and that read can fail. */
export type MemoryLoadState =
  | { status: "loading" }
  | { status: "ready" }
  | { status: "error"; message: string };

const READY: MemoryLoadState = { status: "ready" };

type Props = {
  memos: string[];
  load?: MemoryLoadState;
  onRetry?: () => void;
  busy: boolean;
  onAdd: (memo: string) => Promise<void>;
  onReplace: (memos: string[]) => Promise<void>;
  onClear: () => Promise<void>;
};

type DraftRow = {
  id: number;
  value: string;
  // The server value this row was last seeded from. An edited row no longer
  // equals it, which is exactly why reconciliation cannot match on `value`.
  origin: string;
  // Marked for removal but still listed (struck through, with an Undo) until
  // the change is saved, so a slip of the mouse is not silently final.
  removed?: boolean;
};

export function MemoryPanel({ memos, load = READY, onRetry, busy, onAdd, onReplace, onClear }: Props) {
  const [memo, setMemo] = useState("");
  const [draft, setDraft] = useState<DraftRow[]>(
    () => memos.map((value, id) => ({ id, value, origin: value })),
  );

  // `memos` is the authoritative list the server returned. The draft was
  // seeded from it once and never re-derived, so an entry the server
  // normalized away -- trimmed to nothing, or a case-insensitive duplicate --
  // stayed on screen looking saved.
  //
  // Re-seeding wholesale fixed that and broke something else: adding a memory
  // also changes the server's answer, so every *other* row the user had edited
  // and not yet saved silently reverted. Reconcile instead -- carry each
  // surviving row's in-progress value across by matching on the server value
  // it came from, and build a fresh row only for genuinely new entries.
  const lastServerMemos = useRef(memos);
  useEffect(() => {
    const previous = lastServerMemos.current;
    const changed =
      previous.length !== memos.length || previous.some((value, index) => value !== memos[index]);
    if (!changed) return;
    lastServerMemos.current = memos;
    setDraft((current) => {
      const byOrigin = new Map<string, DraftRow[]>();
      for (const row of current) {
        const bucket = byOrigin.get(row.origin);
        if (bucket) bucket.push(row);
        else byOrigin.set(row.origin, [row]);
      }
      let nextId = current.reduce((highest, row) => Math.max(highest, row.id), -1) + 1;
      return memos.map((value) => {
        // shift(), so duplicate server values claim distinct rows.
        const existing = byOrigin.get(value)?.shift();
        return existing
          ? { ...existing, origin: value }
          : { id: nextId++, value, origin: value };
      });
    });
  }, [memos]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = memo.trim();
    if (!value) return;
    void onAdd(value).then(() => {
      setDraft((current) => {
        if (current.some((item) => item.value.toLocaleLowerCase() === value.toLocaleLowerCase())) return current;
        const id = current.reduce((highest, item) => Math.max(highest, item.id), -1) + 1;
        return [...current, { id, value, origin: value }];
      });
      setMemo("");
    }).catch(() => undefined);
  };

  const [clearDialogOpen, setClearDialogOpen] = useState(false);
  const [clearing, setClearing] = useState(false);

  // The dialog stays up while the clear runs and closes either way: a failure
  // is reported by the workspace, and the list is left exactly as it was.
  const confirmClear = async () => {
    if (clearing) return;
    setClearing(true);
    try {
      await onClear();
      setDraft([]);
    } catch {
      // Reported by the workspace callback; the draft is kept.
    } finally {
      setClearing(false);
      setClearDialogOpen(false);
    }
  };

  const setRemoved = (id: number, removed: boolean) => {
    setDraft((current) => current.map((row) => row.id === id ? { ...row, removed } : row));
  };

  // Anything the server has not seen: an edited value or a pending removal.
  // Rows added through the form are saved by then and carry `value === origin`.
  // The server trims what it stores, so a trailing space alone is no change --
  // and would otherwise stay flagged as unsaved after a save that trimmed it.
  const dirty = draft.some((row) => row.removed || row.value.trim() !== row.origin);

  const saveChanges = () => {
    // Failures are reported by the workspace callback; the draft is kept.
    void onReplace(draft.filter((row) => !row.removed).map((row) => row.value)).catch(() => undefined);
  };

  const heading = (
    <div className="panel-heading">
      <div>
        <p className="eyebrow">PERMANENT MEMORY</p>
        <h2 id="memory-title">Remembered facts</h2>
      </div>
    </div>
  );

  // With nothing loaded there is no list to edit, and an empty one would read
  // as "no memories" -- and invite a save that replaces the real ones.
  if (load.status === "loading") {
    return (
      <section className="panel" aria-labelledby="memory-title">
        {heading}
        <p className="empty-state" role="status">Loading saved memories...</p>
      </section>
    );
  }
  if (load.status === "error") {
    return (
      <section className="panel" aria-labelledby="memory-title">
        {heading}
        <div className="stack-lg">
          <p className="field-error" role="alert">{load.message}</p>
          {onRetry && (
            <button className="button button-secondary" type="button" onClick={onRetry}>
              <RefreshCw aria-hidden="true" size={16} /> Retry
            </button>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="panel" aria-labelledby="memory-title">
      {heading}
      <form className="inline-form" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="new-memory">New memory</label>
        <input id="new-memory" value={memo} onChange={(event) => setMemo(event.target.value)} placeholder="Add a fact" maxLength={500} />
        <button className="icon-button" aria-label="Add memory" disabled={busy || !memo.trim()}>
          <Plus aria-hidden="true" size={17} />
        </button>
      </form>
      {draft.length ? (
        <ul className="memory-list">
          {draft.map((item, index) => (
            <li key={item.id} className={`memory-list-item${item.removed ? " memory-list-item-removed" : ""}`}>
              <input
                aria-label={item.removed ? `Memory ${index + 1} (removed)` : `Memory ${index + 1}`}
                value={item.value}
                maxLength={500}
                readOnly={item.removed}
                onChange={(event) => setDraft((current) => current.map((row) => row.id === item.id ? { ...row, value: event.target.value } : row))}
              />
              {item.removed ? (
                <button className="button button-quiet memory-undo" type="button" aria-label={`Undo removing memory ${index + 1}`} onClick={() => setRemoved(item.id, false)} disabled={busy}><Undo2 aria-hidden="true" size={15} /> Undo</button>
              ) : (
                <button className="icon-button icon-button-small danger-icon" type="button" aria-label={`Remove memory ${index + 1}`} onClick={() => setRemoved(item.id, true)} disabled={busy}><Trash2 aria-hidden="true" size={15} /></button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty-state">No permanent memories stored.</p>
      )}
      <div className="memory-actions">
        {memos.length > 0 && <button className="button button-secondary" type="button" onClick={saveChanges} disabled={busy || !dirty}><Save aria-hidden="true" size={16} /> Save changes</button>}
        {draft.length > 0 && <button className="button button-quiet danger-action" type="button" onClick={() => setClearDialogOpen(true)} disabled={busy}><Eraser aria-hidden="true" size={16} /> Clear all</button>}
        {/* Always present, so a screen reader hears the text appear. */}
        <span className="memory-unsaved" role="status">{dirty ? "Unsaved changes" : ""}</span>
      </div>
      {clearDialogOpen && <ClearMemoriesDialog busy={busy || clearing} onClose={() => setClearDialogOpen(false)} onConfirm={confirmClear} />}
    </section>
  );
}

/**
 * The app's own confirmation rather than window.confirm(), which blocks the
 * whole JavaScript thread until the native dialog is dismissed (ChatPage's
 * MemoryClearConfirmDialog explains the same choice).
 */
function ClearMemoriesDialog({ busy, onClose, onConfirm }: { busy: boolean; onClose: () => void; onConfirm: () => Promise<void> }) {
  return (
    <AlertDialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent>
        <AlertDialog.Title>Clear all memories?</AlertDialog.Title>
        <AlertDialog.Description className="delete-dialog-description">
          This removes every remembered fact, including edits you have not saved. It cannot be undone.
        </AlertDialog.Description>
        <div className="dialog-actions">
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Keep memories</button>
          <button type="button" className="button button-danger" onClick={() => void onConfirm()} disabled={busy}>
            {busy ? "Clearing…" : "Clear all memories"}
          </button>
        </div>
      </DialogContent>
    </AlertDialog.Root>
  );
}
