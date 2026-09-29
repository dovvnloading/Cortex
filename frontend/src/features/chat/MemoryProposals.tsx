import { useId, useLayoutEffect, useRef, useState } from "react";
import { useChatStore } from "../../stores/useChatStore";

type Props = {
  messageId: string;
  memos: readonly string[];
  /** Stores one suggestion. Resolves true only when it was actually saved. */
  onSave: (memo: string) => Promise<boolean>;
};

/**
 * What the model suggested remembering, shown under the answer it came from.
 *
 * A suggestion is only text on screen until the user presses Save, which is
 * the one place it is sent to the memories API. Dismiss just forgets it. Both
 * remove the row, so the card never keeps asking about a fact the user has
 * already decided on.
 */
export function MemoryProposals({ messageId, memos, onSave }: Props) {
  const headingId = useId();
  const listRef = useRef<HTMLUListElement>(null);
  const savingRef = useRef(false);
  const focusRowRef = useRef<number | null>(null);
  const [saving, setSaving] = useState(false);

  // Removing a row would drop keyboard focus onto the page. Hand it to the row
  // that took the removed one's place instead (the last row's neighbour).
  useLayoutEffect(() => {
    const index = focusRowRef.current;
    if (index === null) return;
    focusRowRef.current = null;
    const saveButtons = listRef.current?.querySelectorAll<HTMLButtonElement>("button[data-memory-save]");
    if (!saveButtons?.length) return;
    saveButtons[Math.min(index, saveButtons.length - 1)]?.focus();
  }, [memos]);

  const dismiss = (memo: string, index: number) => {
    focusRowRef.current = index;
    useChatStore.getState().dismissProposedMemory(messageId, memo);
  };

  const save = async (memo: string, index: number) => {
    // Two quick presses must not store the same fact twice.
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      if (await onSave(memo)) dismiss(memo, index);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <section className="memory-proposals" aria-labelledby={headingId}>
      <p id={headingId} className="memory-proposals-heading">Cortex suggests remembering</p>
      <ul className="memory-proposals-list" ref={listRef}>
        {memos.map((memo, index) => {
          const textId = `${headingId}-memo-${index}`;
          return (
            <li className="memory-proposal" key={memo}>
              <span id={textId} className="memory-proposal-text">{memo}</span>
              <span className="memory-proposal-actions">
                <button
                  type="button"
                  className="button button-secondary"
                  data-memory-save=""
                  aria-describedby={textId}
                  disabled={saving}
                  onClick={() => void save(memo, index)}
                >
                  Save
                </button>
                <button
                  type="button"
                  className="button button-quiet"
                  aria-describedby={textId}
                  disabled={saving}
                  onClick={() => dismiss(memo, index)}
                >
                  Dismiss
                </button>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
