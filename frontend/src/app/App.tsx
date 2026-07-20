import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import type { ChatResponse, ChatSummary, CortexSettings, JobAccepted, MemoryResponse, ModelResponse, SSEEvent, SystemResponse } from "../../../contracts/cortex-api";
import { CortexApi, ApiError } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ChatPage } from "../components/ChatPage";
import { Onboarding } from "../components/Onboarding";
import { SettingsPanel } from "../components/SettingsPanel";
import { useToast } from "./ToastProvider";

type Props = { api?: CortexApi };

function readBootstrapToken(): string {
  const searchToken = new URLSearchParams(window.location.search).get("bootstrap");
  if (searchToken) return searchToken;
  return new URLSearchParams(window.location.hash.replace(/^#/, "")).get("bootstrap") ?? "";
}

export function App({ api: providedApi }: Props) {
  const [api] = useState(() => providedApi ?? new CortexApi());
  const [sessionReady, setSessionReady] = useState(api.hasSession);
  const [bootstrapToken] = useState(readBootstrapToken);
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
  const [models, setModels] = useState<ModelResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelProgress, setModelProgress] = useState<{ model: string; status: string; percent: number | null } | null>(null);
  const [theme, setTheme] = useState<"light" | "dark" | "system">("system");

  const quitCortex = async () => {
    if (!window.confirm("Quit Cortex?")) return;
    try {
      await api.shutdown();
    } catch (error) {
      notify(apiMessage(error, "Could not shut down Cortex."), "error");
    }
  };

  const loadWorkspace = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [systemResponse, chatResponse, settingsResponse, memoryResponse, modelResponse] = await Promise.all([
        api.system(),
        api.chats(),
        api.settings(),
        api.memories(),
        api.models(),
      ]);
      setSystem(systemResponse);
      setChats(chatResponse);
      setActiveChatId((current) => current && chatResponse.some((chat) => chat.id === current) ? current : chatResponse[0]?.id ?? null);
      setSettings(settingsResponse.settings);
      setTheme(settingsResponse.settings.appearance?.theme ?? "system");
      setMemos(memoryResponse.memos);
      setModels(modelResponse);
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

  const replaceMemory = async (next: string[]) => {
    setMemoryBusy(true);
    try {
      const response = await api.replaceMemories(next);
      setMemos(response.memos);
      notify("Memory changes saved.", "success");
    } catch (error) { notify(apiMessage(error, "Could not save memory changes."), "error"); }
    finally { setMemoryBusy(false); }
  };

  const runModelJob = async (accepted: JobAccepted, model = "required models") => {
    setModelBusy(true);
    setModelProgress({ model, status: "Starting…", percent: null });
    try {
      await api.streamJob(accepted.job_id, (event) => updateModelProgress(event, setModelProgress));
      setModels(await api.models());
      notify("Model operation completed.", "success");
    } catch (error) { notify(apiMessage(error, "Model operation failed."), "error"); }
    finally { setModelBusy(false); }
  };

  const checkModels = async () => {
    try { await runModelJob(await api.checkModels()); }
    catch (error) { notify(apiMessage(error, "Could not check Ollama models."), "error"); }
  };

  const pullModel = async (model: string) => {
    try { await runModelJob(await api.pullModel(model), model); }
    catch (error) { notify(apiMessage(error, "Could not start the model pull."), "error"); }
  };

  if (loading) return <main className="loading-state" aria-live="polite"><span className="loading-spinner" />Loading local workspace…</main>;
  if (loadError || !system || !settings || !models) {
    return <main className="fatal-state"><h1>Workspace unavailable</h1><p>{loadError ?? "Cortex returned an incomplete workspace."}</p><button className="button button-primary" onClick={() => void loadWorkspace()}>Retry</button></main>;
  }

  return (
    <BrowserRouter>
      <AppShell chats={chats} activeChatId={activeChatId} system={system} theme={theme} onThemeChange={setTheme} onSelectChat={setActiveChatId} onRenameChat={renameChat} onDeleteChat={deleteChat} onQuit={quitCortex}>
        <Routes>
          <Route path="/settings" element={<SettingsPanel settings={settings} memos={memos} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onReplaceMemory={replaceMemory} onClearMemory={clearMemory} models={models} modelBusy={modelBusy} modelProgress={modelProgress} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onCheckModels={checkModels} onPullModel={pullModel} />} />
          <Route path="/chat/new" element={<ChatRoute api={api} onChatChanged={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} onForked={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} />} />
          <Route path="/chat/:threadId" element={<ChatRoute api={api} onChatChanged={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} onForked={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} />} />
          <Route path="*" element={<Navigate to="/chat/new" replace />} />
        </Routes>
      </AppShell>
    </BrowserRouter>
  );
}

function updateModelProgress(
  event: SSEEvent,
  setProgress: Dispatch<SetStateAction<{ model: string; status: string; percent: number | null } | null>>,
): void {
  if (event.kind !== "progress") return;
  const data = event.data ?? {};
  const model = typeof data.model === "string" ? data.model : "required models";
  const status = typeof data.message === "string" ? data.message : event.phase ?? "Working";
  const percent = typeof data.percent === "number" ? data.percent : null;
  setProgress({ model, status, percent });
}

function ChatRoute({ api, onChatChanged, onForked }: { api: CortexApi; onChatChanged: (chat: ChatResponse) => void; onForked: (chat: ChatResponse) => void }) {
  const { threadId } = useParams();
  const navigate = useNavigate();
  return <ChatPage api={api} threadId={threadId ?? null} onThreadCreated={(id) => navigate(`/chat/${id}`, { replace: true })} onChatChanged={onChatChanged} onForked={(chat) => { onForked(chat); navigate(`/chat/${chat.id}`); }} />;
}

function updateChatSummary(setChats: Dispatch<SetStateAction<ChatSummary[]>>, chat: ChatResponse): void {
  setChats((current) => {
    const summary = { id: chat.id, title: chat.title, timestamp: chat.timestamp };
    const next = current.filter((item) => item.id !== chat.id);
    return [summary, ...next];
  });
}

function apiMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}
