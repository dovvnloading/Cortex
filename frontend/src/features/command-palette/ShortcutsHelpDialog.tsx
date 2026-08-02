import { useHotkey } from "../../hooks/useHotkey";
import { Dialog, DialogContent } from "../../shared/ui/Dialog";
import { useUiStore } from "../../stores/useUiStore";

const SHORTCUTS: { keys: string; description: string }[] = [
  { keys: "Ctrl/Cmd + K", description: "Open the command palette" },
  { keys: "?", description: "Show this shortcuts reference" },
  { keys: "Enter", description: "Send a message" },
  { keys: "Shift + Enter", description: "New line in the composer" },
  { keys: "Escape", description: "Stop the response while it's generating" },
];

export function ShortcutsHelpDialog() {
  const open = useUiStore((state) => state.shortcutsDialogOpen);
  const setOpen = useUiStore((state) => state.setShortcutsDialogOpen);
  useHotkey("?", false, () => setOpen(true));

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <DialogContent>
        <Dialog.Title>Keyboard shortcuts</Dialog.Title>
        <dl className="shortcuts-list">
          {SHORTCUTS.map((shortcut) => (
            <div className="shortcuts-row" key={shortcut.keys}>
              <dt><kbd>{shortcut.keys}</kbd></dt>
              <dd>{shortcut.description}</dd>
            </div>
          ))}
        </dl>
        <div className="dialog-actions">
          <button type="button" className="button button-secondary" onClick={() => setOpen(false)}>Close</button>
        </div>
      </DialogContent>
    </Dialog.Root>
  );
}
