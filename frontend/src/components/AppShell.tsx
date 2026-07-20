import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { Menu, Pencil, Plus, Settings, Trash2 } from "lucide-react";
import type { ChatSummary, ModelResponse } from "../../../contracts/cortex-api";

type Props = {
  chats: ChatSummary[];
  activeChatId: string | null;
  modelConnection: ModelResponse["connection"];
  theme: "light" | "dark" | "system";
  onSelectChat: (id: string) => void;
  onRenameChat: (id: string, title: string) => Promise<void>;
  onDeleteChat: (id: string) => Promise<void>;
  children: ReactNode;
};

export function AppShell({
  chats,
  activeChatId,
  modelConnection,
  theme,
  onSelectChat,
  onRenameChat,
  onDeleteChat,
  children,
}: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const [sidebarVisible, setSidebarVisible] = useState(() => !isCompactWindow());
  const [renameTarget, setRenameTarget] = useState<ChatSummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ChatSummary | null>(null);

  const isSettings = location.pathname === "/settings";
  const activeTitle = isSettings
    ? "Settings"
    : chats.find((chat) => chat.id === activeChatId)?.title || "Cortex";

  const closeSidebarOnCompactLayout = () => {
    if (window.matchMedia("(max-width: 760px)").matches) setSidebarVisible(false);
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
    onSelectChat(id);
    navigate(`/chat/${id}`);
    closeSidebarOnCompactLayout();
  };

  return (
    <div className={`app-shell theme-${theme} ${sidebarVisible ? "" : "sidebar-collapsed"}`}>
      <header className="window-bar">
        <button
          className="window-control sidebar-toggle"
          type="button"
          aria-label={sidebarVisible ? "Hide chat history" : "Show chat history"}
          onClick={() => setSidebarVisible((visible) => !visible)}
        >
          <Menu aria-hidden="true" size={17} />
        </button>
        <h1 className="window-title">{activeTitle}</h1>
        <div className="window-actions">
          <NavLink
            to="/settings"
            className={({ isActive }) => `window-control settings-control ${isActive ? "window-control-active" : ""}`}
            aria-label="Settings"
            title={modelConnection?.message ?? "Settings"}
          >
            <Settings aria-hidden="true" size={17} />
            <span className={`connection-indicator ${modelConnection?.success ? "connection-connected" : "connection-error"}`} aria-hidden="true" />
          </NavLink>
        </div>
      </header>

      <div className="workspace-body">
        {sidebarVisible && <button className="sidebar-scrim" aria-label="Close chat history" onClick={() => setSidebarVisible(false)} />}
        <aside className="sidebar" aria-label="Chat history">
          <button className="new-chat-button" type="button" onClick={createChat}>
            <Plus aria-hidden="true" size={16} />
            New Chat
          </button>
          <div className="chat-list" aria-label="Saved chats">
            {chats.length ? chats.map((chat) => (
              <div className={`chat-list-item ${activeChatId === chat.id && !isSettings ? "chat-list-item-active" : ""}`} key={chat.id}>
                <button className="chat-list-select" type="button" onClick={() => selectChat(chat.id)} aria-current={activeChatId === chat.id && !isSettings ? "page" : undefined}>
                  {chat.title || "Untitled chat"}
                </button>
                <div className="chat-list-actions">
                  <button className="history-action" type="button" aria-label={`Rename ${chat.title || "chat"}`} onClick={() => setRenameTarget(chat)}>
                    <Pencil aria-hidden="true" size={13} />
                  </button>
                  <button className="history-action history-action-danger" type="button" aria-label={`Delete ${chat.title || "chat"}`} onClick={() => setDeleteTarget(chat)}>
                    <Trash2 aria-hidden="true" size={13} />
                  </button>
                </div>
              </div>
            )) : <p className="sidebar-empty">No saved conversations yet.</p>}
          </div>
        </aside>

        <main className={`main-content ${isSettings ? "settings-content" : "chat-content"}`}>{children}</main>
      </div>

      {renameTarget && <RenameDialog chat={renameTarget} onClose={() => setRenameTarget(null)} onSave={onRenameChat} />}
      {deleteTarget && <DeleteChatDialog chat={deleteTarget} onClose={() => setDeleteTarget(null)} onConfirm={async () => { await onDeleteChat(deleteTarget.id); setDeleteTarget(null); }} />}
    </div>
  );
}

function isCompactWindow(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(max-width: 760px)").matches;
}

function RenameDialog({ chat, onClose, onSave }: { chat: ChatSummary; onClose: () => void; onSave: (id: string, title: string) => Promise<void> }) {
  const [title, setTitle] = useState(chat.title);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (title.trim()) void onSave(chat.id, title.trim()).then(onClose);
  };
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="rename-title">
        <h2 id="rename-title">Rename chat</h2>
        <form onSubmit={submit} className="stack-lg">
          <label className="field-label" htmlFor="rename-chat">Chat title
            <input id="rename-chat" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus maxLength={200} />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="button button-primary" disabled={!title.trim()}>Save title</button>
          </div>
        </form>
      </div>
    </div>
  );
}

function DeleteChatDialog({ chat, onClose, onConfirm }: { chat: ChatSummary; onClose: () => void; onConfirm: () => Promise<void> }) {
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const title = chat.title.trim() || "Untitled chat";
  const confirmed = confirmation.trim() === title;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [busy, onClose]);

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
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target && !busy) onClose(); }}>
      <div className="dialog delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-chat-title" aria-describedby="delete-chat-description">
        <div className="delete-dialog-heading">
          <div className="delete-dialog-icon" aria-hidden="true"><Trash2 size={18} /></div>
          <div>
            <p className="eyebrow">PERMANENT ACTION</p>
            <h2 id="delete-chat-title">Delete this chat?</h2>
          </div>
        </div>
        <p id="delete-chat-description" className="delete-dialog-description">This permanently removes the conversation and all of its messages. Deleted chats cannot be recovered.</p>
        <div className="delete-dialog-target"><span>Chat to delete</span><strong>{title}</strong></div>
        <form onSubmit={submit} className="stack-lg">
          <label className="field-label" htmlFor="delete-chat-confirmation">Type <span className="delete-confirm-title">{title}</span> to confirm
            <input id="delete-chat-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoFocus autoComplete="off" spellCheck={false} placeholder={title} />
          </label>
          <div className="dialog-actions">
            <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>Keep chat</button>
            <button type="submit" className="button button-danger" disabled={!confirmed || busy}>{busy ? "Deleting…" : "Delete permanently"}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
