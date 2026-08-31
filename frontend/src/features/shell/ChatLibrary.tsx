import { useMemo, useRef, useState, type FormEvent } from "react";
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

  const hasAnything = filtered.length > 0 || groups.length > 0;

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
  if (groups.length === 0) return null;
  const title = displayChatTitle(chat.title);

  const choose = (groupId: string | null) => {
    onMove(groupId);
    onOpenChange(false);
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
        className="history-action"
        type="button"
        aria-label={`Move ${title} to a group`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
      >
        <MoreHorizontal aria-hidden="true" size={13} />
      </button>
      {open && (
        <div className="chat-row-menu-list" role="menu" aria-label={`Move ${title} to a group`}>
          {groups.map((group) => (
            <button
              key={group.id}
              className="chat-row-menu-item"
              type="button"
              role="menuitem"
              disabled={chat.group_id === group.id}
              onClick={() => choose(group.id)}
            >
              {group.name}
            </button>
          ))}
          {chat.group_id && (
            <button className="chat-row-menu-item" type="button" role="menuitem" onClick={() => choose(null)}>
              Remove from group
            </button>
          )}
        </div>
      )}
    </div>
  );
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
