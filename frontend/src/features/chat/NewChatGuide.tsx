import { displayModelName } from "../../lib/localModels";

/**
 * What a new thread shows before the first message: which model will answer,
 * the few keys worth knowing, and that files can be attached. Deliberately
 * quiet -- no hero, no suggested prompts -- and it goes away as soon as the
 * first message starts.
 */
export function NewChatGuide({ selectedModel }: { selectedModel: string | null }) {
  return (
    <div className="new-chat-guide">
      <p>
        {selectedModel
          ? <>Chatting with <strong>{displayModelName(selectedModel)}</strong></>
          : "No model is selected yet. Choose one with the model picker under the message box."}
      </p>
      <ul className="new-chat-guide-hints" aria-label="Keyboard shortcuts">
        <li><kbd>Enter</kbd> sends, <kbd>Shift</kbd> + <kbd>Enter</kbd> starts a new line</li>
        <li><kbd>Ctrl</kbd>/<kbd>Cmd</kbd> + <kbd>K</kbd> opens the command palette</li>
        <li><kbd>?</kbd> lists every shortcut</li>
      </ul>
      <p className="new-chat-guide-note">Drop, paste or attach images and text or code files to include them.</p>
    </div>
  );
}
