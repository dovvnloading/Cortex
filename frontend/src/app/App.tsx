import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import type { ChatSummary, CortexSettings, MemoryResponse, SystemResponse } from "../../../contracts/cortex-api";
import { CortexApi, ApiError } from "../api/client";
import { AppShell } from "../components/AppShell";
import { Onboarding } from "../components/Onboarding";
import { SettingsPanel } from "../components/SettingsPanel";
import { SystemStatusCard } from "../components/SystemStatusCard";
import { useToast } from "./ToastProvider";

type Props = { api?: CortexApi };

export function App({ api: providedApi }: Props) {
  const [api] = useState(() => providedApi ?? new CortexApi());
  const [sessionReady, setSessionReady] = useState(api.hasSession);
  const [bootstrapToken] = useState(() => new URLSearchParams(window.location.search).get("bootstrap") ?? "");
  const [onboardingError, setOnboardingError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const handleSessionExpired = useCallback(() => setSessionReady(false), []);

  if (!sessionReady) {
    return (
      <Onboarding
        initialToken={bootstrapToken}
        error={onboardingError}
        busy={connecting}
        onSubmit={async (token) => {
          setConnecting(true);
          setOnboardingError(null);
          try {
            await api.exchangeBootstrapToken(token);
            window.history.replaceState({}, "", window.location.pathname);
            setSessionReady(true);
          } catch (error) {
            setOnboardingError(error instanceof ApiError ? error.detail : "Could not connect to Cortex.");
          } finally {
            setConnecting(false);
          }
        }}
      />
    );
  }

  return <AuthenticatedWorkspace api={api} onSessionExpired={handleSessionExpired} />;
}

function AuthenticatedWorkspace({ api, onSessionExpired }: { api: CortexApi; onSessionExpired: () => void }) {
  const { notify } = useToast();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [system, setSystem] = useState<SystemResponse | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [settings, setSettings] = useState<CortexSettings | null>(null);
  const [memos, setMemos] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark" | "system">("system");

  const loadWorkspace = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [systemResponse, chatResponse, settingsResponse, memoryResponse] = await Promise.all([
        api.system(),
        api.chats(),
        api.settings(),
        api.memories(),
      ]);
      setSystem(systemResponse);
      setChats(chatResponse);
      setActiveChatId((current) => current && chatResponse.some((chat) => chat.id === current) ? current : chatResponse[0]?.id ?? null);
      setSettings(settingsResponse.settings);
      setTheme(settingsResponse.settings.appearance?.theme ?? "system");
      setMemos(memoryResponse.memos);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSessionExpired();
      }
      setLoadError(error instanceof ApiError ? error.detail : "Could not load the Cortex workspace.");
    } finally {
      setLoading(false);
    }
  }, [api, onSessionExpired]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadWorkspace(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadWorkspace]);

  useEffect(() => {
    const resolved = theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : theme === "system" ? "light" : theme;
    document.documentElement.dataset.theme = resolved;
  }, [theme]);

  const createChat = async () => {
    try {
      const chat = await api.createChat();
      setChats((current) => [chat, ...current]);
      setActiveChatId(chat.id);
      notify("New chat created.", "success");
    } catch (error) { notify(apiMessage(error, "Could not create chat."), "error"); }
  };

  const renameChat = async (id: string, title: string) => {
    try {
      const chat = await api.renameChat(id, title);
      setChats((current) => current.map((item) => item.id === id ? { ...item, title: chat.title, timestamp: chat.timestamp } : item));
      notify("Chat renamed.", "success");
    } catch (error) { notify(apiMessage(error, "Could not rename chat."), "error"); }
  };

  const deleteChat = async (id: string) => {
    try {
      await api.deleteChat(id);
      setChats((current) => {
        const next = current.filter((chat) => chat.id !== id);
        setActiveChatId((active) => active === id ? next[0]?.id ?? null : active);
        return next;
      });
      notify("Chat deleted.", "success");
    } catch (error) { notify(apiMessage(error, "Could not delete chat."), "error"); }
  };

  const saveSettings = async (next: CortexSettings) => {
    setSaving(true);
    try {
      const response = await api.updateSettings({ settings: next });
      setSettings(response.settings);
      setTheme(response.settings.appearance?.theme ?? "system");
      notify("Settings saved.", "success");
    } catch (error) { notify(apiMessage(error, "Could not save settings."), "error"); }
    finally { setSaving(false); }
  };

  const addMemory = async (memo: string) => {
    setMemoryBusy(true);
    try {
      const response = await api.addMemory(memo);
      setMemos(response.memos);
      notify("Memory saved.", "success");
    } catch (error) { notify(apiMessage(error, "Could not save memory."), "error"); }
    finally { setMemoryBusy(false); }
  };

  const clearMemory = async () => {
    setMemoryBusy(true);
    try {
      const response: MemoryResponse = await api.clearMemories();
      setMemos(response.memos);
      notify("Permanent memories cleared.", "success");
    } catch (error) { notify(apiMessage(error, "Could not clear memories."), "error"); }
    finally { setMemoryBusy(false); }
  };

  if (loading) return <main className="loading-state" aria-live="polite"><span className="loading-spinner" />Loading local workspace…</main>;
  if (loadError || !system || !settings) {
    return <main className="fatal-state"><h1>Workspace unavailable</h1><p>{loadError ?? "Cortex returned an incomplete workspace."}</p><button className="button button-primary" onClick={() => void loadWorkspace()}>Retry</button></main>;
  }

  return (
    <BrowserRouter>
      <AppShell chats={chats} activeChatId={activeChatId} system={system} theme={theme} onThemeChange={setTheme} onSelectChat={setActiveChatId} onCreateChat={createChat} onRenameChat={renameChat} onDeleteChat={deleteChat}>
        <Routes>
          <Route path="/settings" element={<SettingsPanel settings={settings} memos={memos} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onClearMemory={clearMemory} />} />
          <Route path="*" element={<OverviewPage chats={chats} activeChatId={activeChatId} system={system} />} />
        </Routes>
      </AppShell>
    </BrowserRouter>
  );
}

function OverviewPage({ chats, activeChatId, system }: { chats: ChatSummary[]; activeChatId: string | null; system: SystemResponse }) {
  const activeChat = chats.find((chat) => chat.id === activeChatId);
  return <div className="overview-layout"><div className="page-heading"><div><p className="eyebrow">OVERVIEW</p><h2>Good to see you.</h2><p className="page-lede">Your local Cortex control surface is online and ready.</p></div><Link className="button button-secondary" to="/settings">Open settings</Link></div><div className="overview-grid"><SystemStatusCard system={system} /><section className="panel chat-preview" aria-labelledby="chat-preview-title"><p className="eyebrow">CHAT SHELL</p><h2 id="chat-preview-title">{activeChat?.title ?? "No chat selected"}</h2><p className="page-lede">The workspace shell is ready. Streamed conversation parity arrives in the next focused stage.</p><div className="placeholder-surface"><span className="placeholder-orb" /><span>Choose a saved chat or create a new one.</span></div></section></div></div>;
}

function apiMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}
