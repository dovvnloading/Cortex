import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import type { ChatResponse, ChatSummary, CortexSettings, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, JobAccepted, MemoryResponse, ModelResponse, SSEEvent, SystemResponse } from "../../../contracts/cortex-api";
import { CortexApi, ApiError } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ChatPage } from "../components/ChatPage";
import { LocalSetup } from "../components/LocalSetup";
import { Onboarding } from "../components/Onboarding";
import { SettingsPanel, type SettingsPanelProps } from "../components/SettingsPanel";
import { localModelNames } from "../lib/localModels";
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
  const [bootstrapToken, setBootstrapToken] = useState(readBootstrapToken);
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
            // Bootstrap credentials are one-time handoff tokens. Keep the
            // session token in the API client, but never retain a token that
            // would fail if a later 401 returns us to the onboarding screen.
            setBootstrapToken("");
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
  const [executionTasks, setExecutionTasks] = useState<ExecutionTaskSummary[]>([]);
  const [theme, setTheme] = useState<"light" | "dark" | "system">("dark");

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
      setTheme(settingsResponse.settings.appearance?.theme ?? "dark");
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

  useEffect(() => {
    if (!system?.execution_preview_available) {
      return undefined;
    }
    let disposed = false;
    const refresh = async () => {
      try {
        const response = await api.executionTasks({ includeTerminal: true, limit: 20 });
        if (!disposed) setExecutionTasks(response.tasks);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [api, onSessionExpired, system?.execution_preview_available]);

  const visibleExecutionTasks = system?.execution_preview_available ? executionTasks : [];

  const cancelExecution = async (jobId: string) => {
    try {
      await api.cancelExecution(jobId);
      const response = await api.executionTasks({ includeTerminal: true, limit: 20 });
      setExecutionTasks(response.tasks);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else notify(apiMessage(error, "Could not stop the background task."), "error");
    }
  };

  const decideExecutionApproval = async (
    jobId: string,
    decision: ExecutionApprovalDecisionRequest["decision"],
  ) => {
    try {
      await api.decideExecutionApproval(jobId, decision);
      const response = await api.executionTasks({ includeTerminal: true, limit: 20 });
      setExecutionTasks(response.tasks);
      notify(decision === "approved" ? "Background task approved once." : "Background task denied.", "success");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else notify(apiMessage(error, "Could not record the approval decision."), "error");
    }
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
      setTheme(response.settings.appearance?.theme ?? "dark");
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

  const runModelJob = async (accepted: JobAccepted, model = "local model inventory") => {
    setModelBusy(true);
    setModelProgress({ model, status: "Starting...", percent: null });
    try {
      await api.streamJob(accepted.job_id, (event) => updateModelProgress(event, setModelProgress));
      const refreshedModels = await api.models();
      setModels(refreshedModels);
      if (!refreshedModels.connection?.success) {
        notify(refreshedModels.connection?.message ?? "Cortex could not reach Ollama.", "error");
        return;
      }
      notify(model === "local model inventory" ? "Local model inventory refreshed." : "Model operation completed.", "success");
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

  const chooseLocalModel = async (model: string): Promise<boolean> => {
    if (!settings) return false;
    setSaving(true);
    try {
      const response = await api.updateSettings({
        settings: {
          ...settings,
          models: { ...settings.models, chat: model, title: null },
        },
      });
      setSettings(response.settings);
      setTheme(response.settings.appearance?.theme ?? "dark");
      notify(`${model} is ready for local chat.`, "success");
      return true;
    } catch (error) {
      notify(apiMessage(error, "Could not save the local model selection."), "error");
      return false;
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <main className="loading-state" aria-live="polite"><span className="loading-spinner" />Loading local workspace...</main>;
  if (loadError || !system || !settings || !models) {
    return <main className="fatal-state"><h1>Workspace unavailable</h1><p>{loadError ?? "Cortex returned an incomplete workspace."}</p><button className="button button-primary" onClick={() => void loadWorkspace()}>Retry</button></main>;
  }

  const localModels = localModelNames(models);
  const hasLocalInventory = Array.isArray(models.installed_models) || Array.isArray(models.models);
  const selectedModel = settings.models?.chat?.trim() || null;
  const selectedModelAvailable = Boolean(selectedModel && (!hasLocalInventory || localModels.includes(selectedModel)));
  const runtimeConnected = models.connection?.success ?? true;
  const needsLocalSetup = runtimeConnected
    ? !selectedModelAvailable
    : !selectedModel;

  if (needsLocalSetup) {
    return <LocalSetup models={models} settings={settings} busy={modelBusy || saving} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onRescan={checkModels} onSelectModel={chooseLocalModel} />;
  }

  return (
    <BrowserRouter>
      <AppShell chats={chats} activeChatId={activeChatId} modelConnection={models.connection} theme={theme} executionTasks={visibleExecutionTasks} onCancelExecution={cancelExecution} onDecideExecutionApproval={decideExecutionApproval} onSelectChat={setActiveChatId} onRenameChat={renameChat} onDeleteChat={deleteChat}>
        <Routes>
          <Route path="/settings" element={<SettingsRoute activeChatId={activeChatId} settings={settings} memos={memos} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onReplaceMemory={replaceMemory} onClearMemory={clearMemory} models={models} modelBusy={modelBusy} modelProgress={modelProgress} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onCheckModels={checkModels} onPullModel={pullModel} />} />
          <Route path="/chat/new" element={<ChatRoute api={api} runtimeReady={runtimeConnected && selectedModelAvailable} runtimeMessage={models.connection?.message ?? null} localModels={localModels} selectedModel={selectedModel} modelBusy={modelBusy || saving} onSelectModel={chooseLocalModel} onRescanModels={checkModels} onChatChanged={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} onForked={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} />} />
          <Route path="/chat/:threadId" element={<ChatRoute api={api} runtimeReady={runtimeConnected && selectedModelAvailable} runtimeMessage={models.connection?.message ?? null} localModels={localModels} selectedModel={selectedModel} modelBusy={modelBusy || saving} onSelectModel={chooseLocalModel} onRescanModels={checkModels} onChatChanged={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} onForked={(chat) => { setActiveChatId(chat.id); updateChatSummary(setChats, chat); }} />} />
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
  const model = typeof data.model === "string" ? data.model : "local model inventory";
  const status = typeof data.message === "string" ? data.message : event.phase ?? "Working";
  const percent = typeof data.percent === "number" ? data.percent : null;
  setProgress({ model, status, percent });
}

function ChatRoute({ api, runtimeReady, runtimeMessage, localModels, selectedModel, modelBusy, onSelectModel, onRescanModels, onChatChanged, onForked }: { api: CortexApi; runtimeReady: boolean; runtimeMessage: string | null; localModels: readonly string[]; selectedModel: string | null; modelBusy: boolean; onSelectModel: (model: string) => Promise<boolean>; onRescanModels: () => Promise<void>; onChatChanged: (chat: ChatResponse) => void; onForked: (chat: ChatResponse) => void }) {
  const { threadId } = useParams();
  const navigate = useNavigate();
  return <ChatPage api={api} threadId={threadId ?? null} runtimeReady={runtimeReady} runtimeMessage={runtimeMessage} localModels={localModels} selectedModel={selectedModel} modelBusy={modelBusy} onSelectModel={onSelectModel} onRescanModels={onRescanModels} onThreadCreated={(id) => navigate(`/chat/${id}`, { replace: true })} onChatChanged={onChatChanged} onForked={(chat) => { onForked(chat); navigate(`/chat/${chat.id}`); }} />;
}

function SettingsRoute({ activeChatId, ...props }: Omit<SettingsPanelProps, "onClose"> & { activeChatId: string | null }) {
  const navigate = useNavigate();
  return <SettingsPanel {...props} onClose={() => navigate(activeChatId ? `/chat/${activeChatId}` : "/chat/new")} />;
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
