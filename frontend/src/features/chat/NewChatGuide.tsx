import { useUiStore } from "../../stores/useUiStore";

export function NewChatGuide({ selectedModel }: { selectedModel: string | null }) {
  const setShortcutsDialogOpen = useUiStore((state) => state.setShortcutsDialogOpen);

  return (
    <div className="new-chat-guide">
      <h2 id="chat-title">New thread</h2>
      <p>{selectedModel
        ? "Write a message, or drop an image or text file here."
        : "Choose a model in the message box to get started."}</p>
      <button className="shortcuts-link" type="button" onClick={() => setShortcutsDialogOpen(true)}>
        Keyboard shortcuts <kbd aria-hidden="true">?</kbd>
      </button>
    </div>
  );
}
