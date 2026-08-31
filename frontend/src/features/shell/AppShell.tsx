import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Menu, Plus, Search, Settings, Trash2 } from "lucide-react";
import type { ChatGroup, ChatSummary, CodeExecutionSourceResponse, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, ModelResponse } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
import { chatPath, parseAppRoute, useNavigate, usePathname } from "../../lib/navigation";
import { AlertDialog, Dialog, DialogContent } from "../../shared/ui/Dialog";
import { ExecutionTaskTray, type ExecutionArtifactResult } from "./ExecutionTaskTray";
import { ChatLibrary } from "./ChatLibrary";
import { NavigationLink } from "./NavigationLink";

type Props = {
  chats: ChatSummary[];
  groups: ChatGroup[];
  activeChatId: string | null;
  modelConnection: ModelResponse["connection"];
  theme: "light" | "dark" | "system";
  onOpenSettings: () => void;
  onRenameChat: (id: string, title: string) => Promise<void | boolean>;
  onDeleteChat: (id: string) => Promise<void | boolean>;
  onCreateGroup: (name: string) => Promise<void | boolean>;
  onRenameGroup: (groupId: string, name: string) => Promise<void | boolean>;
  onDeleteGroup: (groupId: string) => Promise<void | boolean>;
  onToggleGroup: (groupId: string, collapsed: boolean) => void;
  onMoveChat: (threadId: string, groupId: string | null) => void;
  executionTasks?: ExecutionTaskSummary[];
  onCancelExecution?: (jobId: string) => Promise<void>;
  onDecideExecutionApproval?: (jobId: string, decision: ExecutionApprovalDecisionRequest["decision"]) => Promise<void>;
  onLoadCodeSource?: (jobId: string) => Promise<CodeExecutionSourceResponse>;
  onDownloadArtifact?: (artifact: ExecutionArtifactResult) => Promise<void>;
  children: ReactNode;
};

export function AppShell({
  chats,
  groups,
  activeChatId,
  modelConnection,
  theme,
  onOpenSettings,
  onRenameChat,
  onDeleteChat,
  onCreateGroup,
  onRenameGroup,
  onDeleteGroup,
  onToggleGroup,
  onMoveChat,
  executionTasks = [],
  onCancelExecution,
  onDecideExecutionApproval,
  onLoadCodeSource,
  onDownloadArtifact,
  children,
}: Props) {
  const navigate = useNavigate();
  const pathname = usePathname();
  const [sidebarVisible, setSidebarVisible] = useState(() => !isCompactWindow());
  const [renameTarget, setRenameTarget] = useState<ChatSummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ChatSummary | null>(null);
  const [chatQuery, setChatQuery] = useState("");

  const isSettings = parseAppRoute(pathname).kind === "settings";
  const activeTitle = isSettings
    ? "Settings"
    : activeChatId
      ? displayChatTitle(chats.find((chat) => chat.id === activeChatId)?.title, "Cortex")
      : "New thread";

  const closeSidebarOnCompactLayout = () => {
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 760px)").matches) {
      setSidebarVisible(false);
    }
  };

  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const compactLayout = window.matchMedia("(max-width: 760px)");
    const closeForCompactLayout = (event: MediaQueryListEvent) => {
      if (event.matches) setSidebarVisible(false);
    };
    compactLayout.addEventListener("change", closeForCompactLayout);
    return () => compactLayout.removeEventListener("change", closeForCompactLayout);
  }, []);

  const createChat = () => {
    navigate("/chat/new");
    closeSidebarOnCompactLayout();
  };

  const selectChat = (id: string) => {
    navigate(chatPath(id));
    closeSidebarOnCompactLayout();
  };

  return (
    <div className={`app-shell theme-${theme} ${sidebarVisible ? "" : "sidebar-collapsed"}`}>
      <header className="window-bar">
        <div className="window-bar-leading">
          <button
            className="window-control sidebar-toggle"
            type="button"
            aria-label={sidebarVisible ? "Hide chat history" : "Show chat history"}
            onClick={() => setSidebarVisible((visible) => !visible)}
          >
            <Menu aria-hidden="true" size={17} />
          </button>
          <div className="window-title-group">
            <span className="window-kicker">Cortex</span>
            <h1 className="window-title">{activeTitle}</h1>
          </div>
        </div>
        <div className="window-actions">
          <NavigationLink
            to="/settings"
            className={`window-control settings-control ${isSettings ? "window-control-active" : ""}`}
            aria-label="Settings"
            aria-current={isSettings ? "page" : undefined}
            title={modelConnection?.message ?? "Settings"}
            onClick={onOpenSettings}
          >
            <Settings aria-hidden="true" size={17} />
            <span className={`connection-indicator ${modelConnection?.success ? "connection-connected" : "connection-error"}`} aria-hidden="true" />
          </NavigationLink>
        </div>
      </header>

      <div className="workspace-body">
        {sidebarVisible && <button className="sidebar-scrim" aria-label="Close chat history" onClick={() => setSidebarVisible(false)} />}
        <aside
          className="sidebar"
          aria-label="Chat history"
          aria-hidden={!sidebarVisible}
          inert={!sidebarVisible ? true : undefined}
        >
          <div className="sidebar-brand">
            <span className="sidebar-brand-mark" aria-hidden="true"><img src="/cortex.svg" alt="" /></span>
            <span className="sidebar-brand-copy"><strong>Cortex</strong></span>
          </div>
          <button className="new-chat-button" type="button" onClick={createChat}>
            <Plus aria-hidden="true" size={16} />
            New thread
          </button>
          {chats.length > 0 && (
            <div className="sidebar-search">
              <Search aria-hidden="true" size={14} />
              <input
                type="search"
                placeholder="Search chats"
                aria-label="Search chats by title"
                value={chatQuery}
                onChange={(event) => setChatQuery(event.target.value)}
              />
            </div>
          )}
          <ChatLibrary
            chats={chats}
            groups={groups}
            activeChatId={activeChatId}
            activeRowVisible={!isSettings}
            query={chatQuery}
            onSelectChat={selectChat}
            onRenameChat={setRenameTarget}
            onDeleteChat={setDeleteTarget}
            onCreateGroup={onCreateGroup}
            onRenameGroup={onRenameGroup}
            onDeleteGroup={onDeleteGroup}
            onToggleGroup={onToggleGroup}
            onMoveChat={onMoveChat}
          />
        </aside>

        <main className={`main-content ${isSettings ? "settings-content" : "chat-content"}`}>{children}</main>
      </div>

      {renameTarget && <RenameDialog chat={renameTarget} onClose={() => setRenameTarget(null)} onSave={onRenameChat} />}
      {deleteTarget && <DeleteChatDialog chat={deleteTarget} onClose={() => setDeleteTarget(null)} onConfirm={async () => {
        const result = await onDeleteChat(deleteTarget.id);
        if (result !== false) setDeleteTarget(null);
        return result;
      }} />}
      <ExecutionTaskTray
        tasks={executionTasks}
        onCancel={onCancelExecution}
        onDecideApproval={onDecideExecutionApproval}
        onLoadCodeSource={onLoadCodeSource}
        onDownloadArtifact={onDownloadArtifact}
      />
    </div>
  );
}

function isCompactWindow(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(max-width: 760px)").matches;
}

function RenameDialog({ chat, onClose, onSave }: { chat: ChatSummary; onClose: () => void; onSave: (id: string, title: string) => Promise<void | boolean> }) {
  const [title, setTitle] = useState(displayChatTitle(chat.title));
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      const result = await onSave(chat.id, title.trim());
      if (result !== false) onClose();
    } catch {
      // A rejected mutation is a failure too: preserve the dialog and input.
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent>
        <Dialog.Title>Rename chat</Dialog.Title>
        <form onSubmit={submit} className="stack-lg">
          <label className="field-label" htmlFor="rename-chat">Chat title
            <input id="rename-chat" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus maxLength={200} disabled={busy} />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
            <button type="submit" className="button button-primary" disabled={!title.trim() || busy}>{busy ? "Saving…" : "Save title"}</button>
          </div>
        </form>
      </DialogContent>
    </Dialog.Root>
  );
}

function DeleteChatDialog({ chat, onClose, onConfirm }: { chat: ChatSummary; onClose: () => void; onConfirm: () => Promise<void | boolean> }) {
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const title = displayChatTitle(chat.title);
  const confirmed = confirmation.trim() === title;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!confirmed || busy) return;
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="dialog delete-dialog">
        <div className="delete-dialog-heading">
          <div className="delete-dialog-icon" aria-hidden="true"><Trash2 size={18} /></div>
          <div>
            <p className="eyebrow">PERMANENT ACTION</p>
            <AlertDialog.Title>Delete this chat?</AlertDialog.Title>
          </div>
        </div>
        <AlertDialog.Description className="delete-dialog-description">This permanently removes the conversation and all of its messages. Deleted chats cannot be recovered.</AlertDialog.Description>
        <div className="delete-dialog-target"><span>Chat to delete</span><strong>{title}</strong></div>
        <form onSubmit={submit} className="stack-lg">
          <label className="field-label" htmlFor="delete-chat-confirmation">Type <span className="delete-confirm-title">{title}</span> to confirm
            <input id="delete-chat-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoFocus autoComplete="off" spellCheck={false} placeholder={title} disabled={busy} />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Keep chat</button>
            <button type="submit" className="button button-danger" disabled={!confirmed || busy}>{busy ? "Deleting…" : "Delete permanently"}</button>
          </div>
        </form>
      </DialogContent>
    </AlertDialog.Root>
  );
}
