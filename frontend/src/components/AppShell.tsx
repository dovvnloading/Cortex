import { useState, type FormEvent, type ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { FilePlus2, LayoutDashboard, LogOut, Menu, Pencil, Settings, Trash2, X } from "lucide-react";
import type { ChatSummary, SystemResponse } from "../../../contracts/cortex-api";

type Props = {
  chats: ChatSummary[];
  activeChatId: string | null;
  system: SystemResponse;
  theme: "light" | "dark" | "system";
  onThemeChange: (theme: "light" | "dark" | "system") => void;
  onSelectChat: (id: string) => void;
  onRenameChat: (id: string, title: string) => Promise<void>;
  onDeleteChat: (id: string) => Promise<void>;
  onQuit: () => Promise<void>;
  children: ReactNode;
};

export function AppShell({
  chats,
  activeChatId,
  system,
  theme,
  onThemeChange,
  onSelectChat,
  onRenameChat,
  onDeleteChat,
  onQuit,
  children,
}: Props) {
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<ChatSummary | null>(null);

  const createChat = () => { navigate("/chat/new"); setSidebarOpen(false); };
  const selectChat = (id: string) => {
    onSelectChat(id);
    navigate(`/chat/${id}`);
    setSidebarOpen(false);
  };

  return (
    <div className={`app-shell theme-${theme}`}>
      <button className="mobile-menu" aria-label="Open navigation" onClick={() => setSidebarOpen(true)}><Menu aria-hidden="true" size={20} /></button>
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`} aria-label="Primary navigation">
        <div className="sidebar-brand"><span className="brand-mark brand-mark-small">C</span><span>Cortex</span><button className="icon-button sidebar-close" aria-label="Close navigation" onClick={() => setSidebarOpen(false)}><X aria-hidden="true" size={18} /></button></div>
        <button className="button button-primary button-wide" onClick={createChat}><FilePlus2 aria-hidden="true" size={17} /> New chat</button>
        <nav className="primary-nav" aria-label="Workspace">
          <NavLink to="/" end className={({ isActive }) => isActive ? "nav-link nav-link-active" : "nav-link"}><LayoutDashboard aria-hidden="true" size={17} /> Overview</NavLink>
          <NavLink to="/settings" className={({ isActive }) => isActive ? "nav-link nav-link-active" : "nav-link"}><Settings aria-hidden="true" size={17} /> Settings</NavLink>
        </nav>
        <div className="sidebar-divider" />
        <div className="sidebar-section-heading"><span>History</span><span className="count-badge">{chats.length}</span></div>
        <div className="chat-list" aria-label="Saved chats">
          {chats.length ? chats.map((chat) => (
            <div className={`chat-list-item ${activeChatId === chat.id ? "chat-list-item-active" : ""}`} key={chat.id}>
              <button className="chat-list-select" onClick={() => selectChat(chat.id)} aria-current={activeChatId === chat.id ? "page" : undefined}>{chat.title || "Untitled chat"}</button>
              <div className="chat-list-actions">
                <button className="icon-button icon-button-small" aria-label={`Rename ${chat.title || "chat"}`} onClick={() => setRenameTarget(chat)}><Pencil aria-hidden="true" size={14} /></button>
                <button className="icon-button icon-button-small danger-icon" aria-label={`Delete ${chat.title || "chat"}`} onClick={() => void onDeleteChat(chat.id)}><Trash2 aria-hidden="true" size={14} /></button>
              </div>
            </div>
          )) : <p className="sidebar-empty">Your saved chats will appear here.</p>}
        </div>
        <div className="sidebar-footer"><span className="connection-dot" aria-hidden="true" /> Local session active <button className="icon-button" aria-label="Quit Cortex" title="Quit Cortex" onClick={() => void onQuit()}><LogOut aria-hidden="true" size={16} /></button></div>
      </aside>
      <div className="content-shell">
        <header className="topbar">
          <div><p className="eyebrow">CORTEX WORKSPACE</p><h1>Local intelligence, under your control.</h1></div>
          <div className="topbar-actions">
            <span className="status-pill status-success"><span className="connection-dot" aria-hidden="true" /> {system.preview ? "Preview" : "Local"}</span>
            <select className="theme-select" aria-label="Theme" value={theme} onChange={(event) => onThemeChange(event.target.value as typeof theme)}><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select>
            <button className="icon-button" aria-label="Quit Cortex" title="Quit Cortex" onClick={() => void onQuit()}><LogOut aria-hidden="true" size={17} /></button>
          </div>
        </header>
        <main className="main-content">{children}</main>
      </div>
      {renameTarget && <RenameDialog chat={renameTarget} onClose={() => setRenameTarget(null)} onSave={onRenameChat} />}
    </div>
  );
}

function RenameDialog({ chat, onClose, onSave }: { chat: ChatSummary; onClose: () => void; onSave: (id: string, title: string) => Promise<void> }) {
  const [title, setTitle] = useState(chat.title);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (title.trim()) void onSave(chat.id, title.trim()).then(onClose);
  };
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><div className="dialog" role="dialog" aria-modal="true" aria-labelledby="rename-title"><h2 id="rename-title">Rename chat</h2><form onSubmit={submit} className="stack-lg"><label className="field-label" htmlFor="rename-chat">Chat title<input id="rename-chat" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus maxLength={200} /></label><div className="dialog-actions"><button type="button" className="button button-quiet" onClick={onClose}>Cancel</button><button type="submit" className="button button-primary" disabled={!title.trim()}>Save title</button></div></form></div></div>;
}
