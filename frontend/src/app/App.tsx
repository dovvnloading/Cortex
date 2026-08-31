import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatResponse, CortexSettings, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, JobAccepted, LlamaCppRuntimeStatus, MemoryResponse, ModelDownloadRequest, ModelResponse, SSEEvent, SystemResponse } from "../../../contracts/cortex-api";
import { CortexApi, ApiError } from "../api/client";
import { AppShell } from "../features/shell/AppShell";
import { CommandPalette } from "../features/command-palette/CommandPalette";
import { ShortcutsHelpDialog } from "../features/command-palette/ShortcutsHelpDialog";
import { ChatPage } from "../features/chat/ChatPage";
import { Onboarding } from "../features/shell/Onboarding";
import { SettingsPanel, type SettingsPanelProps } from "../features/settings/SettingsPanel";
import { displayModelName, isGGUFModel, localModelNames } from "../lib/localModels";
import { chatPath, navigate, parseAppRoute, useNavigate, usePathname } from "../lib/navigation";
import { useChatStore } from "../stores/useChatStore";
import { useModelStore, type ModelProgress } from "../stores/useModelStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useToast } from "./ToastProvider";

type Props = { api?: CortexApi };

const UNAVAILABLE_MODELS: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  missing_models: [],
  optional_missing_models: [],
  models: [],
  connection: {
    success: false,
    status: "error",
    message: "The model service is unavailable. You can continue browsing your workspace.",
  },
};

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

  useEffect(() => api.subscribeSessionExpired(handleSessionExpired), [api, handleSessionExpired]);

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
            setOnboardingError(error instanceof ApiError ? error.detail : "Could not open the local workspace.");
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
  const pathname = usePathname();
  const route = parseAppRoute(pathname);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [system, setSystem] = useState<SystemResponse | null>(null);
  const chats = useChatStore((state) => state.chats);
  const setChats = useChatStore((state) => state.setChats);
  const upsertChatSummary = useChatStore((state) => state.upsertChatSummary);
  const groups = useChatStore((state) => state.groups);
  const setGroups = useChatStore((state) => state.setGroups);
  const upsertGroup = useChatStore((state) => state.upsertGroup);
  const removeGroup = useChatStore((state) => state.removeGroup);
  const setChatGroup = useChatStore((state) => state.setChatGroup);
  const [settingsReturnChatId, setSettingsReturnChatId] = useState<string | null>(null);
  const settings = useSettingsStore((state) => state.settings);
  const setSettings = useSettingsStore((state) => state.setSettings);
  const saving = useSettingsStore((state) => state.saving);
  const setSaving = useSettingsStore((state) => state.setSaving);
  const [memos, setMemos] = useState<string[]>([]);
  const models = useModelStore((state) => state.models);
  const setModels = useModelStore((state) => state.setModels);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const modelBusy = useModelStore((state) => state.modelBusy);
  const setModelBusy = useModelStore((state) => state.setModelBusy);
  const modelProgress = useModelStore((state) => state.modelProgress);
  const setModelProgress = useModelStore((state) => state.setModelProgress);
  const setLlamacppStatus = useModelStore((state) => state.setLlamacppStatus);
  const [executionTasks, setExecutionTasks] = useState<ExecutionTaskSummary[]>([]);
  const [theme, setTheme] = useState<"light" | "dark" | "system">("dark");
  const chatsRef = useRef(chats);
  const executionTaskRefreshRef = useRef<Promise<void> | null>(null);

  useEffect(() => {
    chatsRef.current = chats;
  }, [chats]);

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
      setLlamacppStatus(systemResponse.llamacpp ?? null);
      setChats(chatResponse);
      // Groups are organisation on top of the chats, so they load out of band
      // like the model inventory does: if the endpoint is unavailable the
      // library still opens with every chat present, just ungrouped. Blocking
      // the whole workspace on filing metadata would be the wrong trade.
      void api.chatGroups()
        .then(setGroups)
        .catch((error) => {
          if (error instanceof ApiError && error.status === 401) onSessionExpired();
          else setGroups([]);
        });
      setSettings(settingsResponse.settings);
      setTheme(settingsResponse.settings.appearance?.theme ?? "dark");
      setMemos(memoryResponse.memos);
      setModels(UNAVAILABLE_MODELS);
      void api.models()
        .then(setModels)
        .catch((error) => {
          if (error instanceof ApiError && error.status === 401) onSessionExpired();
          else setModels(UNAVAILABLE_MODELS);
        });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onSessionExpired();
      }
      setLoadError(error instanceof ApiError ? error.detail : "Could not load the local workspace.");
    } finally {
      setLoading(false);
    }
  }, [api, onSessionExpired, setChats, setGroups, setModels, setSettings, setLlamacppStatus]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadWorkspace(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadWorkspace]);

  useEffect(() => {
    const resolved = theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : theme === "system" ? "light" : theme;
    document.documentElement.dataset.theme = resolved;
  }, [theme]);

  useEffect(() => {
    if (route.kind === "not-found") navigate("/chat/new", { replace: true });
  }, [route.kind]);

  const refreshExecutionTasks = useCallback((): Promise<void> => {
    const inFlight = executionTaskRefreshRef.current;
    if (inFlight) return inFlight;

    // Defer the request one microtask so the in-flight marker is installed
    // before an unusually eager fetch implementation can resolve or throw.
    const refresh = Promise.resolve().then(async () => {
      try {
        const response = await api.executionTasks({ includeTerminal: true, limit: 20 });
        setExecutionTasks(response.tasks);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
      }
    });
    executionTaskRefreshRef.current = refresh;
    void refresh.then(
      () => {
        if (executionTaskRefreshRef.current === refresh) executionTaskRefreshRef.current = null;
      },
      () => {
        if (executionTaskRefreshRef.current === refresh) executionTaskRefreshRef.current = null;
      },
    );
    return refresh;
  }, [api, onSessionExpired]);

  useEffect(() => {
    if (!system?.execution_preview_available) {
      return undefined;
    }
    void refreshExecutionTasks();
    const timer = window.setInterval(() => void refreshExecutionTasks(), 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [refreshExecutionTasks, system?.execution_preview_available]);

  // Only poll the local llama.cpp runtime state while a GGUF model is
  // actually selected -- Ollama users never spend cycles on this. A GGUF
  // model can take a while to download/start on first use, and this is
  // what lets the composer show live "loaded" / "starting" state instead
  // of only ever reflecting whatever was true at the last full page load.
  const selectedModelIsGGUF = isGGUFModel(settings?.models?.chat ?? null);
  useEffect(() => {
    if (!selectedModelIsGGUF) return undefined;
    let disposed = false;
    const refresh = async () => {
      try {
        const response = await api.system();
        if (!disposed) setLlamacppStatus(response.llamacpp ?? null);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [api, onSessionExpired, selectedModelIsGGUF, setLlamacppStatus]);

  const visibleExecutionTasks = system?.execution_preview_available
    ? executionTasks.filter((task) => shouldShowExecutionTask(task, system.started_at))
    : [];

  const cancelExecution = async (jobId: string) => {
    try {
      await api.cancelExecution(jobId);
      await refreshExecutionTasks();
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
      await refreshExecutionTasks();
      notify(decision === "approved" ? "Background task approved once." : "Background task denied.", "success");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else notify(apiMessage(error, "Could not record the approval decision."), "error");
    }
  };

  const loadCodeSource = async (jobId: string) => {
    try {
      return await api.executionSource(jobId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      throw error;
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
      const fallbackChatId = chatsRef.current.find((chat) => chat.id !== id)?.id ?? null;
      setChats((current) => current.filter((chat) => chat.id !== id));
      setSettingsReturnChatId((current) => current === id ? fallbackChatId : current);
      const currentRoute = parseAppRoute(window.location.pathname);
      if (currentRoute.kind === "chat" && currentRoute.threadId === id) {
        navigate(fallbackChatId ? chatPath(fallbackChatId) : "/chat/new", { replace: true });
      }
      notify("Chat deleted.", "success");
    } catch (error) { notify(apiMessage(error, "Could not delete chat."), "error"); }
  };

  const createGroup = async (name: string) => {
    try {
      upsertGroup(await api.createChatGroup(name));
      notify("Group created.", "success");
    } catch (error) { notify(apiMessage(error, "Could not create group."), "error"); }
  };

  const renameGroup = async (groupId: string, name: string) => {
    try {
      upsertGroup(await api.updateChatGroup(groupId, { name }));
    } catch (error) { notify(apiMessage(error, "Could not rename group."), "error"); }
  };

  const deleteGroup = async (groupId: string) => {
    try {
      await api.deleteChatGroup(groupId);
      // Local mirror of the server rule: the group goes, its chats stay and
      // reappear in the ungrouped list.
      removeGroup(groupId);
      notify("Group deleted. Its chats moved back to the main list.", "success");
    } catch (error) { notify(apiMessage(error, "Could not delete group."), "error"); }
  };

  const toggleGroup = (groupId: string, collapsed: boolean) => {
    const previous = useChatStore.getState().groups.find((group) => group.id === groupId);
    if (!previous) return;
    // Optimistic: collapsing is a high-frequency, zero-risk interaction and
    // must feel instant. The persisted value is only a preference, so a
    // failed write rolls the row back and stays quiet.
    upsertGroup({ ...previous, collapsed });
    void api.updateChatGroup(groupId, { collapsed }).catch(() => upsertGroup(previous));
  };

  const moveChat = (threadId: string, groupId: string | null) => {
    const previous = useChatStore.getState().chats.find((chat) => chat.id === threadId)?.group_id ?? null;
    setChatGroup(threadId, groupId);
    void api.moveChatToGroup(threadId, groupId).catch((error) => {
      setChatGroup(threadId, previous);
      notify(apiMessage(error, "Could not move chat."), "error");
    });
  };

  const saveSettings = async (next: CortexSettings) => {
    setSaving(true);
    try {
      const response = await api.updateSettings({ settings: next, expected_revision: next.revision });
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

  // `checkOllamaConnection`: the refreshed inventory's `connection` field
  // reflects Ollama's reachability specifically -- a GGUF-only user with no
  // Ollama running should never see an unrelated job (like a successful
  // GGUF download) reported as failed just because Ollama is unreachable.
  // `notifyOnSuccess`: callers that want their own, more specific success
  // message (e.g. "modelname downloaded and selected") suppress the generic
  // one here instead of showing both.
  const runModelJob = async (
    accepted: JobAccepted,
    model = "local model inventory",
    options: { checkOllamaConnection?: boolean; notifyOnSuccess?: boolean } = {},
  ): Promise<Record<string, unknown> | null> => {
    const { checkOllamaConnection = true, notifyOnSuccess = true } = options;
    setModelBusy(true);
    setModelProgress({ model, status: "Starting...", percent: null });
    let completedData: Record<string, unknown> | null = null;
    let failureMessage: string | null = null;
    try {
      await api.streamJob(accepted.job_id, (event) => {
        updateModelProgress(event, setModelProgress);
        if (event.kind === "completed") completedData = event.data ?? null;
        if (event.kind === "error") {
          const message = event.data?.message;
          failureMessage = typeof message === "string" && message ? message : "Model operation failed.";
        }
      });
      if (failureMessage) {
        notify(failureMessage, "error");
        return null;
      }
      const refreshedModels = await api.models();
      setModels(refreshedModels);
      if (checkOllamaConnection && !refreshedModels.connection?.success) {
        notify(refreshedModels.connection?.message ?? "Cortex could not reach Ollama.", "error");
        return completedData;
      }
      if (notifyOnSuccess) {
        notify(model === "local model inventory" ? "Local model inventory refreshed." : "Model operation completed.", "success");
      }
      return completedData;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else notify(apiMessage(error, "Model operation failed."), "error");
      return null;
    } finally { setModelBusy(false); }
  };

  const checkModels = async () => {
    try { await runModelJob(await api.checkModels()); }
    catch (error) { notify(apiMessage(error, "Could not check Ollama models."), "error"); }
  };

  const pullModel = async (model: string) => {
    try { await runModelJob(await api.pullModel(model), model); }
    catch (error) { notify(apiMessage(error, "Could not start the model pull."), "error"); }
  };

  const downloadGGUFModel = async (request: ModelDownloadRequest) => {
    const label = request.source === "huggingface" ? request.filename ?? "GGUF model" : "GGUF model";
    let accepted: JobAccepted;
    try {
      accepted = await api.downloadGGUFModel(request);
    } catch (error) {
      notify(apiMessage(error, "Could not start the model download."), "error");
      throw error;
    }
    const result = await runModelJob(accepted, label, { checkOllamaConnection: false, notifyOnSuccess: false });
    const filename = result && typeof result.filename === "string" ? result.filename : null;
    if (!filename) {
      // runModelJob already showed the specific failure reason as a toast.
      throw new Error("Model download failed.");
    }
    const selected = await chooseLocalModel(`gguf:${filename}`);
    if (!selected) {
      notify(`${filename} downloaded. Select it from the model menu to start chatting.`, "success");
    }
  };

  const chooseLocalModel = async (model: string): Promise<boolean> => {
    // Read the store rather than this render's `settings`: a GGUF download
    // runs for minutes before selecting what it fetched, and Settings stays
    // editable throughout. Sending the snapshot captured when the download
    // started would silently revert everything the user saved meanwhile.
    const current = useSettingsStore.getState().settings;
    if (!current) return false;
    setSaving(true);
    try {
      const response = await api.updateSettings({
        settings: {
          ...current,
          models: { ...current.models, chat: model, title: null },
        },
        expected_revision: current.revision,
      });
      setSettings(response.settings);
      setTheme(response.settings.appearance?.theme ?? "dark");
      notify(`${displayModelName(model)} is ready for local chat.`, "success");
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
  const selectedModelSupportsVision = models.models?.find((model) => model.name === selectedModel)?.supports_vision ?? null;
  const selectedModelAvailable = Boolean(selectedModel && (!hasLocalInventory || localModels.includes(selectedModel)));
  // A GGUF-selected model runs through Cortex's own managed local runtime,
  // not Ollama -- Ollama's connection state is irrelevant to it.
  const runtimeConnected = isGGUFModel(selectedModel) || (models.connection?.success ?? true);
  const llamacppStatus: LlamaCppRuntimeStatus = system.llamacpp ?? { state: "idle", binary_present: false, models_directory: "" };
  const routeChatId = route.kind === "chat" ? route.threadId : null;
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    void saveSettings({ ...settings, appearance: { ...settings.appearance, theme: next } });
  };

  return (
    <>
      <AppShell chats={chats} activeChatId={routeChatId} modelConnection={models.connection} theme={theme} executionTasks={visibleExecutionTasks} onCancelExecution={cancelExecution} onDecideExecutionApproval={decideExecutionApproval} onLoadCodeSource={loadCodeSource} onOpenSettings={() => { if (route.kind === "chat") setSettingsReturnChatId(route.threadId); }} onRenameChat={renameChat} onDeleteChat={deleteChat} groups={groups} onCreateGroup={createGroup} onRenameGroup={renameGroup} onDeleteGroup={deleteGroup} onToggleGroup={toggleGroup} onMoveChat={moveChat}>
        {route.kind === "settings"
          ? <SettingsRoute activeChatId={settingsReturnChatId} settings={settings} memos={memos} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onReplaceMemory={replaceMemory} onClearMemory={clearMemory} models={models} modelBusy={modelBusy} modelProgress={modelProgress} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onCheckModels={checkModels} onPullModel={pullModel} llamacppStatus={llamacppStatus} onDownloadGGUF={downloadGGUFModel} />
          : <ChatRoute threadId={routeChatId} api={api} runtimeReady={runtimeConnected && selectedModelAvailable} runtimeMessage={models.connection?.message ?? null} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={modelBusy || saving} onSelectModel={chooseLocalModel} onRescanModels={checkModels} onChatChanged={upsertChatSummary} onForked={upsertChatSummary} onSessionExpired={onSessionExpired} />}
      </AppShell>
      <CommandPalette
        chats={chats}
        localModels={localModels}
        selectedModel={selectedModel}
        onNewChat={() => navigate("/chat/new")}
        onOpenSettings={() => navigate("/settings")}
        onToggleTheme={toggleTheme}
        onSelectModel={(model) => void chooseLocalModel(model)}
        onSelectChat={(id) => navigate(chatPath(id))}
      />
      <ShortcutsHelpDialog />
    </>
  );
}

function updateModelProgress(event: SSEEvent, setProgress: (progress: ModelProgress) => void): void {
  if (event.kind !== "progress") return;
  const data = event.data ?? {};
  const model = typeof data.model === "string" ? data.model : "local model inventory";
  const status = typeof data.message === "string" ? data.message : event.phase ?? "Working";
  const percent = typeof data.percent === "number" ? data.percent : null;
  setProgress({ model, status, percent });
}

function ChatRoute({ threadId, api, runtimeReady, runtimeMessage, localModels, selectedModel, selectedModelSupportsVision, modelBusy, onSelectModel, onRescanModels, onChatChanged, onForked, onSessionExpired }: { threadId: string | null; api: CortexApi; runtimeReady: boolean; runtimeMessage: string | null; localModels: readonly string[]; selectedModel: string | null; selectedModelSupportsVision: boolean | null; modelBusy: boolean; onSelectModel: (model: string) => Promise<boolean>; onRescanModels: () => Promise<void>; onChatChanged: (chat: ChatResponse) => void; onForked: (chat: ChatResponse) => void; onSessionExpired: () => void }) {
  const navigate = useNavigate();
  return <ChatPage api={api} threadId={threadId} runtimeReady={runtimeReady} runtimeMessage={runtimeMessage} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={modelBusy} onSelectModel={onSelectModel} onRescanModels={onRescanModels} onThreadCreated={(id) => navigate(chatPath(id), { replace: true })} onChatChanged={onChatChanged} onForked={(chat) => { onForked(chat); navigate(chatPath(chat.id)); }} onSessionExpired={onSessionExpired} />;
}

function SettingsRoute({ activeChatId, ...props }: Omit<SettingsPanelProps, "onClose"> & { activeChatId: string | null }) {
  const navigate = useNavigate();
  return <SettingsPanel {...props} onClose={() => navigate(activeChatId ? chatPath(activeChatId) : "/chat/new")} />;
}

const ACTIVE_EXECUTION_STATUSES = new Set<ExecutionTaskSummary["status"]>([
  "queued",
  "running",
  "cancelling",
]);

function shouldShowExecutionTask(task: ExecutionTaskSummary, runtimeStartedAt: string): boolean {
  if (ACTIVE_EXECUTION_STATUSES.has(task.status) || task.approval_state === "pending") {
    return true;
  }
  const taskUpdatedAt = Date.parse(task.updated_at);
  const runtimeStart = Date.parse(runtimeStartedAt);
  // Terminal records are scoped to this backend lifetime. A malformed
  // timestamp cannot be safely scoped, so hide it instead of replaying stale
  // completion notices every time the workspace starts.
  return !Number.isNaN(taskUpdatedAt) && !Number.isNaN(runtimeStart) && taskUpdatedAt >= runtimeStart;
}

function apiMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}
