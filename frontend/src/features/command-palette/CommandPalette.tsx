import { Command, defaultFilter } from "cmdk";
import { Moon, Plus, Settings, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChatSummary } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
import { nextThemePreference, THEME_LABELS, type ThemePreference } from "../../lib/theme";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { useHotkey } from "../../hooks/useHotkey";
import { useUiStore } from "../../stores/useUiStore";

type Props = {
  chats: ChatSummary[];
  localModels: readonly string[];
  selectedModel: string | null;
  /** The saved theme preference; the theme item names the one it would switch to. */
  theme: ThemePreference;
  onNewChat: () => void;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
  onSelectModel: (model: string) => void;
  onSelectChat: (threadId: string) => void;
};

/** With nothing typed, the palette offers this many of the newest chats. */
const RECENT_CHAT_LIMIT = 8;
/** Once something is typed, every chat is searched but at most this many are shown. */
const MAX_CHAT_RESULTS = 50;
/**
 * How long typing must pause before the chats are searched again. Scoring a
 * title costs microseconds, but a long history is thousands of them on every
 * keystroke; the commands and models, a handful, still filter on each key.
 */
const CHAT_SEARCH_DELAY_MS = 120;

const CHAT_VALUE_PREFIX = "chat:";

/**
 * Chat rows carry the chat id as their value -- so two chats with the same
 * title (every untitled chat is "New Chat") are two rows that can each be
 * reached, instead of one highlight shared by both -- and the title as a
 * keyword. Scoring the value would match against the id, so those rows are
 * scored on their title alone; every other row is scored as cmdk would.
 */
function paletteFilter(value: string, search: string, keywords?: string[]): number {
  if (value.startsWith(CHAT_VALUE_PREFIX)) return defaultFilter(keywords?.join(" ") ?? "", search);
  return defaultFilter(value, search, keywords);
}

/** The best matches for `query` among all chats, best first, newest first among equals, capped. */
function searchChats(chats: readonly ChatSummary[], query: string): ChatSummary[] {
  const matches: { chat: ChatSummary; score: number; position: number }[] = [];
  chats.forEach((chat, position) => {
    const score = defaultFilter(displayChatTitle(chat.title), query);
    if (score > 0) matches.push({ chat, score, position });
  });
  matches.sort((a, b) => b.score - a.score || a.position - b.position);
  return matches.slice(0, MAX_CHAT_RESULTS).map((match) => match.chat);
}

export function CommandPalette({
  chats,
  localModels,
  selectedModel,
  theme,
  onNewChat,
  onOpenSettings,
  onToggleTheme,
  onSelectModel,
  onSelectChat,
}: Props) {
  const open = useUiStore((state) => state.commandPaletteOpen);
  const setOpen = useUiStore((state) => state.setCommandPaletteOpen);
  useHotkey("k", true, () => setOpen(!open));

  // Every command closes the palette once it has run.
  const execute = (action: () => void) => {
    action();
    setOpen(false);
  };

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Command palette"
      filter={paletteFilter}
      className="command-palette-root"
      overlayClassName="command-palette-overlay"
      contentClassName="command-palette-content"
    >
      <PaletteBody
        chats={chats}
        localModels={localModels}
        selectedModel={selectedModel}
        theme={theme}
        onNewChat={() => execute(onNewChat)}
        onOpenSettings={() => execute(onOpenSettings)}
        onToggleTheme={() => execute(onToggleTheme)}
        onSelectModel={(model) => execute(() => onSelectModel(model))}
        onSelectChat={(id) => execute(() => onSelectChat(id))}
      />
    </Command.Dialog>
  );
}

/**
 * What is inside the dialog. It exists only while the palette is open, so the
 * text typed into it is forgotten when the palette closes and the next opening
 * starts empty, without any code to reset it.
 */
function PaletteBody({
  chats,
  localModels,
  selectedModel,
  theme,
  onNewChat,
  onOpenSettings,
  onToggleTheme,
  onSelectModel,
  onSelectChat,
}: Props) {
  const [query, setQuery] = useState("");
  const trimmed = query.trim();
  const settledQuery = useDebouncedValue(trimmed, CHAT_SEARCH_DELAY_MS);
  // Clearing the box shows the recent chats at once; only searching waits.
  const chatQuery = trimmed === "" ? "" : settledQuery;
  const searching = chatQuery !== "";
  const visibleChats = useMemo(
    () => (searching ? searchChats(chats, chatQuery) : chats.slice(0, RECENT_CHAT_LIMIT)),
    [chats, chatQuery, searching],
  );
  // Until the search has caught up with what was typed, the chat rows on screen
  // are for an earlier query, and "No results" would be premature.
  const searchPending = trimmed !== chatQuery;

  return (
    <>
      <Command.Input
        value={query}
        onValueChange={setQuery}
        placeholder="Type a command or search chats…"
        className="command-palette-input"
      />
      <Command.List className="command-palette-list">
        {!searchPending && <Command.Empty className="command-palette-empty">No results.</Command.Empty>}
        <Command.Group heading="Chat" className="command-palette-group">
          <Command.Item className="command-palette-item" onSelect={onNewChat}>
            <Plus size={15} aria-hidden="true" /> New chat
          </Command.Item>
          <Command.Item className="command-palette-item" onSelect={onOpenSettings}>
            <Settings size={15} aria-hidden="true" /> Open settings
          </Command.Item>
          <Command.Item
            className="command-palette-item"
            keywords={["theme", "appearance", "light", "dark", "system"]}
            onSelect={onToggleTheme}
          >
            <Moon size={15} aria-hidden="true" /> Change theme
            <span className="command-palette-current">Next: {THEME_LABELS[nextThemePreference(theme)]}</span>
          </Command.Item>
        </Command.Group>
        {localModels.length > 0 && (
          <Command.Group heading="Model" className="command-palette-group">
            {localModels.map((model) => (
              <Command.Item key={model} className="command-palette-item" onSelect={() => onSelectModel(model)}>
                <Sparkles size={15} aria-hidden="true" />
                Switch to {model}
                {model === selectedModel && <span className="command-palette-current">Current</span>}
              </Command.Item>
            ))}
          </Command.Group>
        )}
        {visibleChats.length > 0 && (
          <Command.Group heading={searching ? "Chats" : "Recent chats"} className="command-palette-group">
            {visibleChats.map((chat) => {
              const title = displayChatTitle(chat.title);
              return (
                <Command.Item
                  key={chat.id}
                  className="command-palette-item"
                  value={`chat:${chat.id}`}
                  keywords={[title]}
                  onSelect={() => onSelectChat(chat.id)}
                >
                  {title}
                </Command.Item>
              );
            })}
          </Command.Group>
        )}
      </Command.List>
    </>
  );
}
