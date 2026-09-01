import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ChevronRight, FolderPlus, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import type { ChatGroup, ChatSummary } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
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

/**
 * The chat library: groups (folders/projects) above a flat list of everything
 * ungrouped.
 *
 * Two rules shape this component. Search results are always shown flat and
 * fully expanded -- collapsing a group while the user is searching would hide
 * the very match they are looking for. And a group is only ever filing, so
 * deleting one is a low-stakes action on the group row, never a destructive
 * one that implicates the chats inside it.
 */
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
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return chats;
    return chats.filter((chat) => displayChatTitle(chat.title).toLowerCase().includes(needle));
  }, [chats, query]);

  const byGroup = useMemo(() => {
    const buckets = new Map<string, ChatSummary[]>();
    for (const group of groups) buckets.set(group.id, []);
    const ungrouped: ChatSummary[] = [];
    for (const chat of filtered) {
      const bucket = chat.group_id ? buckets.get(chat.group_id) : undefined;
      if (bucket) bucket.push(chat);
      else ungrouped.push(chat);
    }
    return { buckets, ungrouped };
  }, [filtered, groups]);

  const renderRow = (chat: ChatSummary) => (
    <ChatRow
      key={chat.id}
      chat={chat}
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

  return (
    <div className="chat-library">
      <div className="chat-library-heading">
        <span>Library</span>
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
                  aria-label={`${collapsed ? "Expand" : "Collapse"} ${group.name}`}
                  aria-expanded={!collapsed}
                  onClick={() => onToggleGroup(group.id, !collapsed)}
                >
                  <ChevronRight className="chat-group-chevron" aria-hidden="true" size={13} />
                  <span className="chat-group-name">{group.name}</span>
                  <span className="chat-group-count">{groupChats.length}</span>
                </button>
                <div className="chat-row-actions">
                  <button
                    className="history-action"
                    type="button"
                    aria-label={`Rename group ${group.name}`}
                    onClick={() => setRenameGroupTarget(group)}
                  >
                    <Pencil aria-hidden="true" size={12} />
                  </button>
                  <button
                    className="history-action history-action-danger"
                    type="button"
                    aria-label={`Delete group ${group.name}`}
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

        {byGroup.ungrouped.map(renderRow)}

        {!hasAnything && (
          <p className="sidebar-empty">{chats.length ? "No chats match your search." : "No threads yet."}</p>
        )}
      </div>

      {creatingGroup && (
        <GroupNameDialog
          title="New group"
          submitLabel="Create group"
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
          chatCount={(byGroup.buckets.get(deleteGroupTarget.id) ?? []).length}
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
  groups,
  active,
  onSelect,
  onRename,
  onDelete,
  onMove,
}: {
  chat: ChatSummary;
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
        {title}
      </button>
      <div className="chat-row-actions">
        <MoveToGroupMenu
          chat={chat}
          groups={groups}
          open={menuOpen}
          onOpenChange={setMenuOpen}
          onMove={onMove}
        />
        <button className="history-action" type="button" aria-label={`Rename ${title}`} onClick={onRename}>
          <Pencil aria-hidden="true" size={12} />
        </button>
        <button className="history-action history-action-danger" type="button" aria-label={`Delete ${title}`} onClick={onDelete}>
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
        <MoreHorizontal aria-hidden="true" size={13} />
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
  onClose,
  onSave,
}: {
  title: string;
  submitLabel: string;
  initialValue?: string;
  onClose: () => void;
  onSave: (name: string) => Promise<void | boolean>;
}) {
  const [name, setName] = useState(initialValue);
  const [busy, setBusy] = useState(false);

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
