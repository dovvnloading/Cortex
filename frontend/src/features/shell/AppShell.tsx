import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Menu, Pencil, Plus, Search, Settings, Trash2 } from "lucide-react";
import type { ChatSummary, CodeExecutionSourceResponse, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, ModelResponse } from "../../../../contracts/cortex-api";
import { displayChatTitle } from "../../lib/chatTitle";
import { chatPath, parseAppRoute, useNavigate, usePathname } from "../../lib/navigation";
import { useChatStore } from "../../stores/useChatStore";
import { AlertDialog, Dialog, DialogContent } from "../../shared/ui/Dialog";
import { ExecutionTaskTray } from "./ExecutionTaskTray";
import { ExportTranscriptMenu } from "../chat/ExportTranscriptMenu";
import { NavigationLink } from "./NavigationLink";

type Props = {
  chats: ChatSummary[];
  activeChatId: string | null;
  modelConnection: ModelResponse["connection"];
  theme: "light" | "dark" | "system";
  onOpenSettings: () => void;
  onRenameChat: (id: string, title: string) => Promise<void>;
  onDeleteChat: (id: string) => Promise<void>;
  executionTasks?: ExecutionTaskSummary[];
  onCancelExecution?: (jobId: string) => Promise<void>;
  onDecideExecutionApproval?: (jobId: string, decision: ExecutionApprovalDecisionRequest["decision"]) => Promise<void>;
  onLoadCodeSource?: (jobId: string) => Promise<CodeExecutionSourceResponse>;
  children: ReactNode;
};

export function AppShell({
  chats,
  activeChatId,
  modelConnection,
  theme,
  onOpenSettings,
  onRenameChat,
  onDeleteChat,
  executionTasks = [],
  onCancelExecution,
  onDecideExecutionApproval,
  onLoadCodeSource,
  children,
}: Props) {
  const navigate = useNavigate();
  const pathname = usePathname();
  const activeChat = useChatStore((state) => state.activeChat);
  const [sidebarVisible, setSidebarVisible] = useState(() => !isCompactWindow());
  const [renameTarget, setRenameTarget] = useState<ChatSummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ChatSummary | null>(null);
  const [chatQuery, setChatQuery] = useState("");

  const isSettings = parseAppRoute(pathname).kind === "settings";
  const filteredChats = useMemo(() => {
    const query = chatQuery.trim().toLowerCase();
    if (!query) return chats;
    return chats.filter((chat) => displayChatTitle(chat.title).toLowerCase().includes(query));
  }, [chats, chatQuery]);
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
          {!isSettings && activeChat && <ExportTranscriptMenu chat={activeChat} />}
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
        <aside className="sidebar" aria-label="Chat history">
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
          <div className="sidebar-section-heading"><span>Threads</span><span>{filteredChats.length}</span></div>
          <div className="chat-list" aria-label="Saved chats">
            {filteredChats.length ? filteredChats.map((chat) => (
              <div className={`chat-list-item ${activeChatId === chat.id && !isSettings ? "chat-list-item-active" : ""}`} key={chat.id}>
                <button className="chat-list-select" type="button" onClick={() => selectChat(chat.id)} aria-current={activeChatId === chat.id && !isSettings ? "page" : undefined}>
                  {displayChatTitle(chat.title)}
                </button>
                <div className="chat-list-actions">
                  <button className="history-action" type="button" aria-label={`Rename ${displayChatTitle(chat.title)}`} onClick={() => setRenameTarget(chat)}>
                    <Pencil aria-hidden="true" size={13} />
                  </button>
                  <button className="history-action history-action-danger" type="button" aria-label={`Delete ${displayChatTitle(chat.title)}`} onClick={() => setDeleteTarget(chat)}>
                    <Trash2 aria-hidden="true" size={13} />
                  </button>
                </div>
              </div>
            )) : <p className="sidebar-empty">{chats.length ? "No chats match your search." : "No threads yet."}</p>}
          </div>
        </aside>

        <main className={`main-content ${isSettings ? "settings-content" : "chat-content"}`}>{children}</main>
      </div>

      {renameTarget && <RenameDialog chat={renameTarget} onClose={() => setRenameTarget(null)} onSave={onRenameChat} />}
      {deleteTarget && <DeleteChatDialog chat={deleteTarget} onClose={() => setDeleteTarget(null)} onConfirm={async () => { await onDeleteChat(deleteTarget.id); setDeleteTarget(null); }} />}
      <ExecutionTaskTray
        tasks={executionTasks}
        onCancel={onCancelExecution}
        onDecideApproval={onDecideExecutionApproval}
        onLoadCodeSource={onLoadCodeSource}
      />
    </div>
  );
}

function isCompactWindow(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(max-width: 760px)").matches;
}

function RenameDialog({ chat, onClose, onSave }: { chat: ChatSummary; onClose: () => void; onSave: (id: string, title: string) => Promise<void> }) {
  const [title, setTitle] = useState(displayChatTitle(chat.title));
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (title.trim()) void onSave(chat.id, title.trim()).then(onClose);
  };
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent>
        <Dialog.Title>Rename chat</Dialog.Title>
        <form onSubmit={submit} className="stack-lg">
          <label className="field-label" htmlFor="rename-chat">Chat title
            <input id="rename-chat" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus maxLength={200} />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="button button-primary" disabled={!title.trim()}>Save title</button>
          </div>
        </form>
      </DialogContent>
    </Dialog.Root>
  );
}

function DeleteChatDialog({ chat, onClose, onConfirm }: { chat: ChatSummary; onClose: () => void; onConfirm: () => Promise<void> }) {
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
