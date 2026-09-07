import { Eraser, Plus, Trash2, Save } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

type Props = {
  memos: string[];
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
};

export function MemoryPanel({ memos, busy, onAdd, onReplace, onClear }: Props) {
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

  const handleClear = () => {
    if (window.confirm("Clear all permanent memories? This cannot be undone.")) {
      void onClear().then(() => setDraft([])).catch(() => undefined);
    }
  };

  return (
    <section className="panel" aria-labelledby="memory-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">PERMANENT MEMORY</p>
          <h2 id="memory-title">Remembered facts</h2>
        </div>
      </div>
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
            <li key={item.id} className="memory-list-item">
              <input aria-label={`Memory ${index + 1}`} value={item.value} maxLength={500} onChange={(event) => setDraft((current) => current.map((row) => row.id === item.id ? { ...row, value: event.target.value } : row))} />
              <button className="icon-button icon-button-small danger-icon" aria-label={`Remove memory ${index + 1}`} onClick={() => setDraft((current) => current.filter((row) => row.id !== item.id))} disabled={busy}><Trash2 aria-hidden="true" size={15} /></button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty-state">No permanent memories stored.</p>
      )}
      <div className="memory-actions">
        {memos.length > 0 && <button className="button button-secondary" onClick={() => void onReplace(draft.map((item) => item.value))} disabled={busy}><Save aria-hidden="true" size={16} /> Save changes</button>}
        {draft.length > 0 && <button className="button button-quiet danger-action" onClick={handleClear} disabled={busy}><Eraser aria-hidden="true" size={16} /> Clear all</button>}
      </div>
    </section>
  );
}
