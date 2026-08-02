import { Command } from "cmdk";
import { Moon, Plus, Settings, Sparkles } from "lucide-react";
import type { ChatSummary } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
import { useHotkey } from "../../hooks/useHotkey";
import { useUiStore } from "../../stores/useUiStore";

type Props = {
  chats: ChatSummary[];
  localModels: readonly string[];
  selectedModel: string | null;
  onNewChat: () => void;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
  onSelectModel: (model: string) => void;
  onSelectChat: (threadId: string) => void;
};

const RECENT_CHAT_LIMIT = 8;

export function CommandPalette({
  chats,
  localModels,
  selectedModel,
  onNewChat,
  onOpenSettings,
  onToggleTheme,
  onSelectModel,
  onSelectChat,
}: Props) {
  const open = useUiStore((state) => state.commandPaletteOpen);
  const setOpen = useUiStore((state) => state.setCommandPaletteOpen);
  useHotkey("k", true, () => setOpen(!open));

  const close = () => setOpen(false);
  const run = (action: () => void) => () => {
    action();
    close();
  };

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Command palette"
      className="command-palette-root"
      overlayClassName="command-palette-overlay"
      contentClassName="command-palette-content"
    >
      <Command.Input placeholder="Type a command or search chats…" className="command-palette-input" />
      <Command.List className="command-palette-list">
        <Command.Empty className="command-palette-empty">No results.</Command.Empty>
        <Command.Group heading="Chat" className="command-palette-group">
          <Command.Item className="command-palette-item" onSelect={run(onNewChat)}>
            <Plus size={15} aria-hidden="true" /> New chat
          </Command.Item>
          <Command.Item className="command-palette-item" onSelect={run(onOpenSettings)}>
            <Settings size={15} aria-hidden="true" /> Open settings
          </Command.Item>
          <Command.Item className="command-palette-item" onSelect={run(onToggleTheme)}>
            <Moon size={15} aria-hidden="true" /> Toggle theme
          </Command.Item>
        </Command.Group>
        {localModels.length > 0 && (
          <Command.Group heading="Model" className="command-palette-group">
            {localModels.map((model) => (
              <Command.Item key={model} className="command-palette-item" onSelect={run(() => onSelectModel(model))}>
                <Sparkles size={15} aria-hidden="true" />
                Switch to {model}
                {model === selectedModel && <span className="command-palette-current">Current</span>}
              </Command.Item>
            ))}
          </Command.Group>
        )}
        {chats.length > 0 && (
          <Command.Group heading="Recent chats" className="command-palette-group">
            {chats.slice(0, RECENT_CHAT_LIMIT).map((chat) => (
              <Command.Item key={chat.id} className="command-palette-item" onSelect={run(() => onSelectChat(chat.id))}>
                {displayChatTitle(chat.title)}
              </Command.Item>
            ))}
          </Command.Group>
        )}
      </Command.List>
    </Command.Dialog>
  );
}
