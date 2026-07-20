import { Brain, Eraser, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

type Props = {
  memos: string[];
  busy: boolean;
  onAdd: (memo: string) => Promise<void>;
  onClear: () => Promise<void>;
};

export function MemoryPanel({ memos, busy, onAdd, onClear }: Props) {
  const [memo, setMemo] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = memo.trim();
    if (!value) return;
    void onAdd(value).then(() => setMemo(""));
  };

  const handleClear = () => {
    if (window.confirm("Clear all permanent memories? This cannot be undone.")) {
      void onClear();
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
      {memos.length ? (
        <ul className="memory-list">
          {memos.map((item) => <li key={item}>{item}</li>)}
        </ul>
      ) : (
        <p className="empty-state">No permanent memories stored.</p>
      )}
      {memos.length > 0 && (
        <button className="button button-quiet danger-action" onClick={handleClear} disabled={busy}>
          <Eraser aria-hidden="true" size={16} /> Clear all memories
        </button>
      )}
    </section>
  );
}
