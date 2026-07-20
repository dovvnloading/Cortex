import { Brain, Eraser, Plus, Trash2, Save } from "lucide-react";
import { useState, type FormEvent } from "react";

type Props = {
  memos: string[];
  busy: boolean;
  onAdd: (memo: string) => Promise<void>;
  onReplace: (memos: string[]) => Promise<void>;
  onClear: () => Promise<void>;
};

export function MemoryPanel({ memos, busy, onAdd, onReplace, onClear }: Props) {
  const [memo, setMemo] = useState("");
  const [draft, setDraft] = useState(memos);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = memo.trim();
    if (!value) return;
    void onAdd(value).then(() => {
      setDraft((current) => current.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase()) ? current : [...current, value]);
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
        <Brain aria-hidden="true" size={19} />
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
            <li key={`${index}-${item}`} className="memory-list-item">
              <input aria-label={`Memory ${index + 1}`} value={item} maxLength={500} onChange={(event) => setDraft((current) => current.map((value, itemIndex) => itemIndex === index ? event.target.value : value))} />
              <button className="icon-button icon-button-small danger-icon" aria-label={`Remove memory ${index + 1}`} onClick={() => setDraft((current) => current.filter((_, itemIndex) => itemIndex !== index))} disabled={busy}><Trash2 aria-hidden="true" size={15} /></button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty-state">No permanent memories stored.</p>
      )}
      <div className="memory-actions">
        {memos.length > 0 && <button className="button button-secondary" onClick={() => void onReplace(draft)} disabled={busy}><Save aria-hidden="true" size={16} /> Save changes</button>}
        {draft.length > 0 && <button className="button button-quiet danger-action" onClick={handleClear} disabled={busy}><Eraser aria-hidden="true" size={16} /> Clear all</button>}
      </div>
    </section>
  );
}
