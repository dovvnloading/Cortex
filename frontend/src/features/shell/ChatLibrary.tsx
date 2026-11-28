import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Archive, ChevronRight, Folder, FolderInput, FolderPlus, Pencil, Search, Trash2 } from "lucide-react";
import type { ChatGroup, ChatSummary } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
import { abandonGroupCreate, readPendingGroupCreate } from "../../lib/pendingGroupCreate";
import { parseMessageTimestamp } from "../../lib/messageTime";
import { Dialog, DialogContent } from "../../shared/ui/Dialog";

export type ChatLibraryProps = {
  chats: ChatSummary[];
  groups: ChatGroup[];
  activeChatId: string | null;
  /** Suppresses the active-row treatment while a non-chat route is showing. */
  activeRowVisible: boolean;
  query: string;
  onSelectChat: (id: string) => void;
  onRenameChat: (chat: ChatSummary) => void;
  onDeleteChat: (chat: ChatSummary) => void;
  onCreateGroup: (name: string) => Promise<void | boolean>;
  onRenameGroup: (groupId: string, name: string) => Promise<void | boolean>;
  onDeleteGroup: (groupId: string) => Promise<void | boolean>;
  onToggleGroup: (groupId: string, collapsed: boolean) => void;
  onMoveChat: (threadId: string, groupId: string | null) => void;
};

type LibraryEntry = { chat: ChatSummary; date: Date | null };

const monthHeading = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const rowDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const fullDate = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "medium" });

function groupByMonth(entries: LibraryEntry[]) {
  const months = new Map<number, { label: string; entries: LibraryEntry[] }>();
  for (const entry of entries) {
    const date = entry.date;
    const key = date ? date.getFullYear() * 12 + date.getMonth() : -Infinity;
    const month = months.get(key);
    if (month) month.entries.push(entry);
    else months.set(key, { label: date ? monthHeading.format(date) : "Date unavailable", entries: [entry] });
  }
  return [...months].sort(([left], [right]) => right - left);
}

export function ChatLibrary({
  chats,
  groups,
  activeChatId,
  activeRowVisible,
  query,
  onSelectChat,
  onRenameChat,
  onDeleteChat,
  onCreateGroup,
  onRenameGroup,
  onDeleteGroup,
  onToggleGroup,
  onMoveChat,
}: ChatLibraryProps) {
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [renameGroupTarget, setRenameGroupTarget] = useState<ChatGroup | null>(null);
  const [deleteGroupTarget, setDeleteGroupTarget] = useState<ChatGroup | null>(null);

  const searching = query.trim().length > 0;
  const entries = useMemo(() => chats.map((chat) => {
    const timestamp = parseMessageTimestamp(chat.timestamp);
    return { chat, date: timestamp === null ? null : new Date(timestamp) };
  }), [chats]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter(({ chat }) => displayChatTitle(chat.title).toLowerCase().includes(needle));
  }, [entries, query]);

  const byGroup = useMemo(() => {
    const buckets = new Map<string, LibraryEntry[]>();
    for (const group of groups) buckets.set(group.id, []);
    const ungrouped: LibraryEntry[] = [];
    for (const entry of filtered) {
      const groupId = entry.chat.group_id;
      const bucket = groupId ? buckets.get(groupId) : undefined;
      if (bucket) bucket.push(entry);
      else ungrouped.push(entry);
    }
    return { buckets, ungrouped };
  }, [filtered, groups]);
  const months = useMemo(() => searching ? [] : groupByMonth(byGroup.ungrouped), [searching, byGroup.ungrouped]);

  const renderRow = ({ chat, date }: LibraryEntry) => (
    <ChatRow
      key={chat.id}
      chat={chat}
      date={date}
      groups={groups}
      active={activeChatId === chat.id && activeRowVisible}
      onSelect={() => onSelectChat(chat.id)}
      onRename={() => onRenameChat(chat)}
      onDelete={() => onDeleteChat(chat)}
      onMove={(groupId) => onMoveChat(chat.id, groupId)}
    />
  );

  // During a search, only matching chats produce visible rows; groups without
  // matches are omitted above and must not suppress the empty search state.
  const hasAnything = searching ? filtered.length > 0 : filtered.length > 0 || groups.length > 0;
  const countNoun = searching ? "result" : "thread";
  const EmptyIcon = searching ? Search : Archive;

  return (
    <div className="chat-library">
      <div className="chat-library-heading">
        <h3>{searching ? "Search results" : "Library"}</h3>
        <span className="chat-library-count" role="status">
          {filtered.length}<span className="sr-only"> {countNoun}{filtered.length === 1 ? "" : "s"}</span>
        </span>
        <button
          className="chat-library-add-group"
          type="button"
          aria-label="New group"
          title="New group"
          onClick={() => setCreatingGroup(true)}
        >
          <FolderPlus aria-hidden="true" size={14} />
        </button>
      </div>

      <div className="chat-list" aria-label="Saved chats">
        {groups.map((group) => {
          const groupChats = byGroup.buckets.get(group.id) ?? [];
          // While searching, a group with no match is noise; one with a match
          // is force-opened so the result is actually reachable.
          if (searching && groupChats.length === 0) return null;
          const collapsed = searching ? false : group.collapsed;
          return (
            <section className="chat-group" key={group.id}>
              <div className={`chat-group-header ${collapsed ? "" : "chat-group-header-open"}`}>
                <button
                  className="chat-group-toggle"
                  type="button"
                  // Explicit, so the control announces the action rather than
                  // reading out "Research 3" from its own contents.
                  aria-label={searching
                    ? `${group.name} is expanded for search`
                    : `${collapsed ? "Expand" : "Collapse"} ${group.name}`}
                  title={searching ? "Search results stay expanded. Clear the search to change this preference." : undefined}
                  aria-expanded={!collapsed}
                  disabled={searching}
                  onClick={() => onToggleGroup(group.id, !collapsed)}
                >
                  <ChevronRight className="chat-group-chevron" aria-hidden="true" size={13} />
                  <Folder className="chat-group-icon" aria-hidden="true" size={15} />
                  <span className="chat-group-name">{group.name}</span>
                  <span className="chat-group-count">{groupChats.length}</span>
                </button>
                <div className="chat-row-actions">
                  <button
                    className="history-action"
                    type="button"
                    aria-label={`Rename group ${group.name}`}
                    title="Rename group"
                    onClick={() => setRenameGroupTarget(group)}
                  >
                    <Pencil aria-hidden="true" size={12} />
                  </button>
                  <button
                    className="history-action history-action-danger"
                    type="button"
                    aria-label={`Delete group ${group.name}`}
                    title="Delete group"
                    onClick={() => setDeleteGroupTarget(group)}
                  >
                    <Trash2 aria-hidden="true" size={12} />
                  </button>
                </div>
              </div>
              {!collapsed && (
                <div className="chat-group-body">
                  {groupChats.length
                    ? groupChats.map(renderRow)
                    : <p className="chat-group-empty">Empty — move a chat here.</p>}
                </div>
              )}
            </section>
          );
        })}

        {searching ? byGroup.ungrouped.map(renderRow) : months.map(([key, month]) => (
          <section className="chat-month" key={key}>
            <h4 className="chat-month-heading">{month.label}</h4>
            {month.entries.map(renderRow)}
          </section>
        ))}

        {!hasAnything && (
          <p className="sidebar-empty">
            <EmptyIcon aria-hidden="true" size={24} />
            {searching ? "No chats match your search." : "No threads yet."}
          </p>
        )}
      </div>

      {creatingGroup && (
        <GroupNameDialog
          title="New group"
          submitLabel="Create group"
          // An unconfirmed create is the only one that can go next: start
          // from its name, and let the user give it up explicitly (#695).
          initialValue={readPendingGroupCreate()?.name ?? ""}
          pendingName={readPendingGroupCreate()?.name ?? null}
          onAbandonPending={abandonGroupCreate}
          onClose={() => setCreatingGroup(false)}
          onSave={onCreateGroup}
        />
      )}
      {renameGroupTarget && (
        <GroupNameDialog
          title="Rename group"
          submitLabel="Save name"
          initialValue={renameGroupTarget.name}
          onClose={() => setRenameGroupTarget(null)}
          onSave={(name) => onRenameGroup(renameGroupTarget.id, name)}
        />
      )}
      {deleteGroupTarget && (
        <DeleteGroupDialog
          group={deleteGroupTarget}
          // Confirmation describes every chat that will be moved, not just
          // those that currently match the sidebar search filter.
          chatCount={chats.filter((chat) => chat.group_id === deleteGroupTarget.id).length}
          onClose={() => setDeleteGroupTarget(null)}
          onConfirm={async () => {
            const result = await onDeleteGroup(deleteGroupTarget.id);
            if (result !== false) setDeleteGroupTarget(null);
            return result;
          }}
        />
      )}
    </div>
  );
}

function ChatRow({
  chat,
  date,
  groups,
  active,
  onSelect,
  onRename,
  onDelete,
  onMove,
}: {
  chat: ChatSummary;
  date: Date | null;
  groups: ChatGroup[];
  active: boolean;
  onSelect: () => void;
  onRename: () => void;
  onDelete: () => void;
  onMove: (groupId: string | null) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const title = displayChatTitle(chat.title);

  return (
    <div className={`chat-row ${active ? "chat-row-active" : ""}`}>
      <button
        className="chat-row-select"
        type="button"
        title={title}
        onClick={onSelect}
        aria-current={active ? "page" : undefined}
      >
        <span className="chat-row-title">{title}</span>
      </button>
      {date && <time className="chat-row-date" dateTime={date.toISOString()} title={fullDate.format(date)}>{rowDate.format(date)}</time>}
      <div className="chat-row-actions">
        <MoveToGroupMenu
          chat={chat}
          groups={groups}
          open={menuOpen}
          onOpenChange={setMenuOpen}
          onMove={onMove}
        />
        <button className="history-action" type="button" aria-label={`Rename ${title}`} title="Rename thread" onClick={onRename}>
          <Pencil aria-hidden="true" size={12} />
        </button>
        <button className="history-action history-action-danger" type="button" aria-label={`Delete ${title}`} title="Delete thread" onClick={onDelete}>
          <Trash2 aria-hidden="true" size={12} />
        </button>
      </div>
    </div>
  );
}

/**
 * A menu rather than drag-and-drop: it is reachable by keyboard and screen
 * reader, works identically on a touch screen, and cannot strand a chat
 * mid-drag. (Pointer dragging can be layered on later without changing this
 * contract.)
 */
function MoveToGroupMenu({
  chat,
  groups,
  open,
  onOpenChange,
  onMove,
}: {
  chat: ChatSummary;
  groups: ChatGroup[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMove: (groupId: string | null) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const typeaheadRef = useRef("");
  const typeaheadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const initialFocusRef = useRef<"first" | "last">("first");
  const menuId = useId();
  const title = displayChatTitle(chat.title);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    const trigger = triggerRef.current;
    const list = trigger?.closest(".chat-list");
    if (!open || !menu || !trigger || !list) return;

    const position = () => {
      const bounds = list.getBoundingClientRect();
      const anchor = trigger.getBoundingClientRect();
      const below = bounds.bottom - anchor.bottom - 4;
      const above = anchor.top - bounds.top - 4;
      const preferredHeight = Math.min(240, menu.scrollHeight + menu.offsetHeight - menu.clientHeight);
      const aboveTrigger = below < preferredHeight && above > below;
      menu.dataset.side = aboveTrigger ? "top" : "bottom";
      menu.style.setProperty("--menu-space", `${Math.max(0, aboveTrigger ? above : below)}px`);
    };

    position();
    const observer = new ResizeObserver(position);
    observer.observe(list);
    list.addEventListener("scroll", position);
    return () => {
      observer.disconnect();
      list.removeEventListener("scroll", position);
    };
  }, [open, groups.length, chat.group_id]);

  useEffect(() => {
    if (!open) {
      typeaheadRef.current = "";
      if (typeaheadTimerRef.current) {
        clearTimeout(typeaheadTimerRef.current);
        typeaheadTimerRef.current = null;
      }
      return;
    }
    const items = getEnabledMenuItems(menuRef.current);
    const item = initialFocusRef.current === "last" ? items.at(-1) : items[0];
    item?.focus();
    if (!item) menuRef.current?.focus();
  }, [open]);

  useEffect(() => () => {
    if (typeaheadTimerRef.current) clearTimeout(typeaheadTimerRef.current);
  }, []);

  if (groups.length === 0) return null;
  const choose = (groupId: string | null) => {
    onMove(groupId);
    onOpenChange(false);
    triggerRef.current?.focus();
  };

  const closeFromKeyboard = () => {
    onOpenChange(false);
    triggerRef.current?.focus();
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = getEnabledMenuItems(menuRef.current);
    if (event.key === "Escape") {
      event.preventDefault();
      closeFromKeyboard();
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      const next = event.shiftKey ? triggerRef.current : getAdjacentFocusable(rootRef.current);
      (next ?? triggerRef.current)?.focus();
      onOpenChange(false);
      return;
    }
    if (!items.length) return;

    const activeIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      const nextIndex = activeIndex < 0
        ? direction === 1 ? 0 : items.length - 1
        : (activeIndex + direction + items.length) % items.length;
      items[nextIndex]?.focus();
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      (event.key === "Home" ? items[0] : items.at(-1))?.focus();
      return;
    }

    // Menu items are group names, so a small typeahead buffer is useful when
    // a library has enough groups that arrowing through them is cumbersome.
    if (event.key.length === 1 && !/\s/.test(event.key) && !event.altKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      typeaheadRef.current = `${typeaheadRef.current}${event.key.toLowerCase()}`;
      if (typeaheadTimerRef.current) clearTimeout(typeaheadTimerRef.current);
      typeaheadTimerRef.current = setTimeout(() => { typeaheadRef.current = ""; }, 500);
      const start = activeIndex < 0 ? 0 : activeIndex + 1;
      const ordered = [...items.slice(start), ...items.slice(0, start)];
      const match = ordered.find((item) => item.textContent?.trim().toLowerCase().startsWith(typeaheadRef.current));
      match?.focus();
    }
  };

  return (
    <div
      className="chat-row-menu"
      ref={rootRef}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) onOpenChange(false);
      }}
    >
      <button
        ref={triggerRef}
        className="history-action"
        type="button"
        aria-label={`Move ${title} to a group`}
        aria-haspopup="menu"
        title="Move to group"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => {
          initialFocusRef.current = "first";
          onOpenChange(!open);
        }}
        onKeyDown={(event) => {
          if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            initialFocusRef.current = event.key === "ArrowDown" ? "first" : "last";
            onOpenChange(true);
          }
        }}
      >
        <FolderInput aria-hidden="true" size={14} />
      </button>
      {open && (
        <div
          className="chat-row-menu-list"
          id={menuId}
          ref={menuRef}
          role="menu"
          tabIndex={-1}
          aria-label={`Move ${title} to a group`}
          onKeyDown={handleMenuKeyDown}
        >
          {groups.map((group) => (
            <button
              key={group.id}
              className="chat-row-menu-item"
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={chat.group_id === group.id}
              onClick={() => choose(group.id)}
            >
              {group.name}
            </button>
          ))}
          {chat.group_id && (
            <button
              className="chat-row-menu-item"
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => choose(null)}
            >
              Remove from group
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function getEnabledMenuItems(menu: HTMLDivElement | null): HTMLButtonElement[] {
  return menu
    ? Array.from(menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'))
    : [];
}

const focusableSelector = [
  "button:not(:disabled)",
  "[href]",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function getAdjacentFocusable(root: HTMLDivElement | null): HTMLElement | null {
  const sibling = root?.nextElementSibling;
  if (!sibling) return null;
  if (sibling instanceof HTMLElement && sibling.matches(focusableSelector)) return sibling;
  return sibling.querySelector<HTMLElement>(focusableSelector);
}

function GroupNameDialog({
  title,
  submitLabel,
  initialValue = "",
  pendingName = null,
  onAbandonPending,
  onClose,
  onSave,
}: {
  title: string;
  submitLabel: string;
  initialValue?: string;
  /** The name of an earlier create whose outcome is unknown. */
  pendingName?: string | null;
  onAbandonPending?: () => void;
  onClose: () => void;
  onSave: (name: string) => Promise<void | boolean>;
}) {
  const [name, setName] = useState(initialValue);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(pendingName);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const result = await onSave(name.trim());
      if (result !== false) onClose();
    } catch {
      // A rejected mutation is a failure too: preserve the dialog and input.
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog.Root open onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <DialogContent>
        <Dialog.Title>{title}</Dialog.Title>
        <form onSubmit={submit} className="stack-lg">
          {pending && (
            <div role="status" className="stack-sm">
              <p>
                Creating the group &ldquo;{pending}&rdquo; was not confirmed, so it may already exist.
                Create it again to recover it without a duplicate, or abandon it to create a different group.
              </p>
              <button
                type="button"
                className="button button-secondary"
                disabled={busy}
                onClick={() => {
                  onAbandonPending?.();
                  setPending(null);
                  if (name.trim() === pending) setName("");
                }}
              >
                Abandon &ldquo;{pending}&rdquo;
              </button>
            </div>
          )}
          <label className="field-label" htmlFor="group-name">Group name
            <input
              id="group-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              autoFocus
              maxLength={120}
              placeholder="Research"
              disabled={busy}
            />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
            <button type="submit" className="button button-primary" disabled={!name.trim() || busy}>{submitLabel}</button>
          </div>
        </form>
      </DialogContent>
    </Dialog.Root>
  );
}

/**
 * Deliberately a plain confirm, not the type-the-name gauntlet the chat
 * delete uses: this removes only the folder, and the chats inside it survive.
 * Matching that friction would misrepresent the stakes.
 */
function DeleteGroupDialog({
  group,
  chatCount,
  onClose,
  onConfirm,
}: {
  group: ChatGroup;
  chatCount: number;
  onClose: () => void;
  onConfirm: () => Promise<void | boolean>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog.Root open onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <DialogContent>
        <Dialog.Title>Delete this group?</Dialog.Title>
        <p className="delete-dialog-description">
          {chatCount === 0
            ? `“${group.name}” is empty. Deleting it changes nothing else.`
            : `The ${chatCount === 1 ? "chat" : `${chatCount} chats`} in “${group.name}” will move back to the main list. Nothing is deleted.`}
        </p>
        <div className="dialog-actions">
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Keep group</button>
          <button
            type="button"
            className="button button-danger"
            disabled={busy}
            onClick={async () => {
              if (busy) return;
              setBusy(true);
              try { await onConfirm(); } catch { /* Preserve the dialog on failure. */ } finally { setBusy(false); }
            }}
          >
            {busy ? "Deleting…" : "Delete group"}
          </button>
        </div>
      </DialogContent>
    </Dialog.Root>
  );
}
