import { Command, defaultFilter } from "cmdk";
import { Moon, Plus, Settings, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ChatSummary } from "../../../../contracts/cortex-api";
import { cloudModelProvider, displayModelName } from "../../lib/localModels";
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
  onSelectModel: (model: string) => void | boolean | Promise<void | boolean>;
  modelSelectionDisabled?: boolean;
  onSelectChat: (threadId: string) => void;
};

/** With nothing typed, the palette offers this many of the newest chats. */
const RECENT_CHAT_LIMIT = 8;
/** Once something is typed, every chat is searched but at most this many are shown. */
const MAX_CHAT_RESULTS = 50;
/**
 * How long typing must pause before the chats are searched again. Scoring a
 * title costs microseconds, but a long history is thousands of them on every
 * keystroke. Hosted model catalogs can also contain thousands of entries.
 */
const CHAT_SEARCH_DELAY_MS = 120;

const CHAT_VALUE_PREFIX = "chat:";
const MODEL_VALUE_PREFIX = "model:";
const MODEL_PREVIEW_LIMIT = 8;
const MAX_MODEL_RESULTS = 50;

function modelCommand(model: string): string {
  return cloudModelProvider(model) ? `Switch to ${displayModelName(model)} (cloud)` : `Switch to ${model}`;
}

function searchModels(models: readonly string[], current: string | null, query: string): string[] {
  if (!query) {
    const preview = models.slice(0, MODEL_PREVIEW_LIMIT);
    if (current && models.includes(current) && !preview.includes(current)) preview[preview.length - 1] = current;
    return preview;
  }
  return models.map((model) => ({ model, score: defaultFilter(modelCommand(model), query) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_MODEL_RESULTS)
    .map(({ model }) => model);
}

/**
 * Chat rows carry the chat id as their value -- so two chats with the same
 * title (every untitled chat is "New Chat") are two rows that can each be
 * reached, instead of one highlight shared by both -- and the title as a
 * keyword. Scoring the value would match against the id, so those rows are
 * scored on their title alone; every other row is scored as cmdk would.
 */
function paletteFilter(value: string, search: string, keywords?: string[]): number {
  if (value.startsWith(CHAT_VALUE_PREFIX) || value.startsWith(MODEL_VALUE_PREFIX)) return defaultFilter(keywords?.join(" ") ?? "", search);
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
  modelSelectionDisabled,
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
        onSelectModel={onSelectModel}
        modelSelectionDisabled={modelSelectionDisabled}
        onSelectChat={(id) => execute(() => onSelectChat(id))}
        onClose={() => setOpen(false)}
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
  modelSelectionDisabled,
  onSelectChat,
  onClose,
}: Props & { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const pendingSelection = useRef(false);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const chooseModel = async (model: string) => {
    if (modelSelectionDisabled || pendingSelection.current) return;
    if (model === selectedModel) { onClose(); return; }
    pendingSelection.current = true;
    try {
      if (await onSelectModel(model) !== false && mounted.current) onClose();
    } catch {
      // The workspace presents the save error. Preserve the search for a retry.
    } finally {
      pendingSelection.current = false;
    }
  };

  const trimmed = query.trim();
  const settledQuery = useDebouncedValue(trimmed, CHAT_SEARCH_DELAY_MS);
  // Clearing the box shows the recent chats at once; only searching waits.
  const chatQuery = trimmed === "" ? "" : settledQuery;
  const searching = chatQuery !== "";
  const visibleChats = useMemo(
    () => (searching ? searchChats(chats, chatQuery) : chats.slice(0, RECENT_CHAT_LIMIT)),
    [chats, chatQuery, searching],
  );
  const visibleModels = useMemo(() => searchModels(localModels, selectedModel, chatQuery), [localModels, selectedModel, chatQuery]);
  // Until the search has caught up with what was typed, the chat rows on screen
  // are for an earlier query, and "No results" would be premature.
  const searchPending = trimmed !== chatQuery;

  return (
    <>
      <Command.Input
        value={query}
        onValueChange={setQuery}
        placeholder="Search commands, models, and chats…"
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
        {visibleModels.length > 0 && (
          <Command.Group heading="Model" className="command-palette-group">
            {visibleModels.map((model) => (
              <Command.Item key={model} className="command-palette-item" value={`${MODEL_VALUE_PREFIX}${model}`}
                keywords={[modelCommand(model)]} disabled={modelSelectionDisabled} onSelect={() => void chooseModel(model)}>
                <Sparkles size={15} aria-hidden="true" />
                <span className="command-palette-model-name">{modelCommand(model)}</span>
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
