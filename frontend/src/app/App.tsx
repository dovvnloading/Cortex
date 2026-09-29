import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatResponse, CortexSettings, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, JobAccepted, JobStatusResponse, LlamaCppRuntimeStatus, MemoryResponse, ModelDownloadRequest, ModelResponse, SSEEvent, SystemResponse } from "../../../contracts/cortex-api";
import {
  CortexApi,
  ApiError,
  describeApiError,
  persistHandoffSecret,
  readPersistedHandoffSecret,
} from "../api/client";
import { AppShell } from "../features/shell/AppShell";
import type { ExecutionArtifactResult } from "../features/shell/ExecutionTaskTray";
import { CommandPalette } from "../features/command-palette/CommandPalette";
import { ShortcutsHelpDialog } from "../features/command-palette/ShortcutsHelpDialog";
const loadChatPage = () => import("../features/chat/ChatPage").then(({ ChatPage: component }) => ({ default: component }));
const ChatPage = lazy(loadChatPage);
import { Onboarding } from "../features/shell/Onboarding";
const SettingsPanel = lazy(() => import("../features/settings/SettingsPanel").then(({ SettingsPanel: component }) => ({ default: component })));
import type { SettingsPanelProps } from "../features/settings/SettingsPanel";
import type { MemoryLoadState } from "../features/settings/MemoryPanel";
import { blockStrayFileDrops } from "../lib/attachments";
import { displayModelName, isGGUFModel, localModelNames } from "../lib/localModels";
import { chatPath, navigate, parseAppRoute, useNavigate, usePathname } from "../lib/navigation";
import { applyStoredTheme, DEFAULT_THEME_PREFERENCE } from "../lib/theme";
import { useAppliedTheme } from "../hooks/useAppliedTheme";
import { useVisiblePolling } from "../hooks/useVisiblePolling";
import { useChatStore } from "../stores/useChatStore";
import { useModelStore, type ModelProgress } from "../stores/useModelStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useToast } from "./ToastProvider";
import { resolveRuntimeAvailability } from "./runtimeAvailability";

type Props = { api?: CortexApi };

/** Statuses an execution task never leaves, so nothing more will change. */
const EXECUTION_TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

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

const DEFAULT_LLAMACPP_STATUS: LlamaCppRuntimeStatus = {
  state: "idle",
  binary_present: false,
  models_directory: "",
};

type LauncherCredentials = { bootstrapToken: string; handoffSecret: string };

/** Read the launcher's one-time credentials. Pure: nothing is written here.
 *
 * The bootstrap token is single-use, so it is only ever read from the URL. The
 * handoff secret is not: it stays valid for the life of the backend process
 * and is the only way to re-exchange an expired session. Falling back to the
 * persisted copy is what makes a reload survivable -- without it, pressing the
 * error boundary's own "Reload workspace" button (or F5) scrubbed the secret,
 * and the next session expiry an hour later left onboarding with no retry at
 * all, so the user had to quit and relaunch Cortex.
 *
 * Storing the secret is a side effect, so it happens in the same effect that
 * scrubs the URL -- see below for why an initialiser must stay pure.
 */
function readLauncherCredentials(): LauncherCredentials {
  const url = new URL(window.location.href);
  const search = url.searchParams;
  const hash = new URLSearchParams(url.hash.replace(/^#/, ""));
  return {
    bootstrapToken: search.get("bootstrap") || hash.get("bootstrap") || "",
    handoffSecret:
      search.get("handoff") || hash.get("handoff") || readPersistedHandoffSecret(),
  };
}

/**
 * Remove the launcher credentials from the visible URL.
 *
 * Separated from the read above because it mutates history, and the read runs
 * from a `useState` initialiser. React documents initialisers as pure and
 * StrictMode double-invokes them in development specifically to surface
 * impure ones; doing the scrub there worked only because React happens to
 * retain the first call's result. Idempotent, so the effect running twice
 * under StrictMode is a no-op the second time.
 *
 * Unrelated query and hash state is preserved for the surrounding shell.
 */
function scrubLauncherCredentials(): void {
  const url = new URL(window.location.href);
  const search = url.searchParams;
  const hash = new URLSearchParams(url.hash.replace(/^#/, ""));

  let changed = false;
  for (const key of ["bootstrap", "handoff"]) {
    if (search.has(key)) {
      search.delete(key);
      changed = true;
    }
    if (hash.has(key)) {
      hash.delete(key);
      changed = true;
    }
  }
  if (!changed) return;

  const nextHash = hash.toString();
  url.hash = nextHash ? `#${nextHash}` : "";
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

export function App({ api: providedApi }: Props) {
  // Until settings load (and on the onboarding screen, which never loads them)
  // the theme is whatever was cached on the last launch. index.html already
  // painted it before any script ran; this keeps the document in step when that
  // inline script did not run, and is what the tests exercise. It never writes
  // the cache -- only a loaded setting does.
  useLayoutEffect(() => {
    applyStoredTheme();
  }, []);
  const [api] = useState(() => providedApi ?? new CortexApi());
  const [sessionReady, setSessionReady] = useState(api.hasSession);
  const [sessionEpoch, setSessionEpoch] = useState(0);
  const [launcherCredentials] = useState(readLauncherCredentials);
  // The read above is pure; the URL scrub is a side effect and belongs here.
  useEffect(() => {
    // All of these are side effects and all are idempotent, so StrictMode's
    // second invocation is a no-op.
    persistHandoffSecret(launcherCredentials.handoffSecret);
    // The client renews an expired session itself, in place, so it needs the
    // secret too. Without this a 401 reached the workspace and remounted it.
    api.setHandoffSecret(launcherCredentials.handoffSecret);
    scrubLauncherCredentials();
  }, [api, launcherCredentials.handoffSecret]);
  // A file dropped outside an attachment target must never navigate the window
  // to it: in the native window that would replace Cortex with the file and
  // leave no way back. Registered here so it covers every screen, onboarding
  // and error states included.
  useEffect(() => blockStrayFileDrops(), []);
  const [bootstrapToken, setBootstrapToken] = useState(launcherCredentials.bootstrapToken);
  const handoffSecret = launcherCredentials.handoffSecret;
  const [onboardingError, setOnboardingError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const reconnectInFlight = useRef<Promise<void> | null>(null);
  const reconnect = useCallback(() => {
    if (!handoffSecret || reconnectInFlight.current) return Promise.resolve();
    const operation = (async () => {
      setConnecting(true);
      setOnboardingError(null);
      try {
        await api.rebootstrap(handoffSecret);
        setSessionEpoch((epoch) => epoch + 1);
        setSessionReady(true);
      } catch (error) {
        setOnboardingError(describeApiError(error, "Could not reopen the local workspace."));
      } finally {
        setConnecting(false);
      }
    })();
    reconnectInFlight.current = operation;
    return operation.finally(() => {
      if (reconnectInFlight.current === operation) reconnectInFlight.current = null;
    });
  }, [api, handoffSecret]);
  // The client renews an expired session in place and only notifies once that
  // failed (or was impossible), so this is the fallback: leave the workspace
  // and try the handoff again from onboarding, which also reports why it
  // failed. The guard makes a late or duplicate call harmless -- a 401 from a
  // request sent under an older token, or a second report after the reconnect
  // already succeeded, must not tear down a session that works.
  const handleSessionExpired = useCallback(() => {
    if (api.hasSession) return;
    setSessionReady(false);
    void reconnect();
  }, [api, reconnect]);

  useEffect(() => api.subscribeSessionExpired(handleSessionExpired), [api, handleSessionExpired]);

  // Start fetching the chat route while the authenticated workspace is
  // loading its initial data. This keeps an active generation's recovery
  // stream from waiting behind the lazy route chunk, without preloading chat
  // code for direct Settings launches.
  useEffect(() => {
    if (sessionReady && parseAppRoute(window.location.pathname).kind === "chat") {
      void loadChatPage().catch(() => undefined);
    }
  }, [sessionReady]);

  if (!sessionReady) {
    return (
      <Onboarding
        initialToken={bootstrapToken}
        error={onboardingError}
        busy={connecting}
        onRetry={handoffSecret ? reconnect : undefined}
        onSubmit={async (token) => {
          setConnecting(true);
          setOnboardingError(null);
          try {
            await api.exchangeBootstrapToken(token);
            // Keep any unrelated route state, but never restore the one-time
            // bootstrap credential after an exchange attempt.
            const cleanUrl = new URL(window.location.href);
            cleanUrl.searchParams.delete("bootstrap");
            const cleanHash = new URLSearchParams(cleanUrl.hash.replace(/^#/, ""));
            cleanHash.delete("bootstrap");
            cleanUrl.hash = cleanHash.toString() ? `#${cleanHash}` : "";
            window.history.replaceState({}, "", `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
            // Bootstrap credentials are one-time handoff tokens. Keep the
            // session token in the API client, but never retain a token that
            // would fail if a later 401 returns us to the onboarding screen.
            setBootstrapToken("");
            setSessionReady(true);
          } catch (error) {
            if (error instanceof ApiError && error.kind === "auth") {
              // The launcher's bootstrap token is single-use and short-lived,
              // so a 401 means this one is spent or stale and submitting it
              // again can never work. Forget it so no retry offers it, and
              // when the handoff secret is still known ask the running
              // backend for a fresh one instead.
              setBootstrapToken("");
              if (handoffSecret) {
                await reconnect();
                return;
              }
            }
            setOnboardingError(describeApiError(error, "Could not open the local workspace."));
          } finally {
            setConnecting(false);
          }
        }}
      />
    );
  }

  return <AuthenticatedWorkspace key={sessionEpoch} api={api} onSessionExpired={handleSessionExpired} />;
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
  const [memoryLoad, setMemoryLoad] = useState<MemoryLoadState>({ status: "loading" });
  const models = useModelStore((state) => state.models);
  const setModels = useModelStore((state) => state.setModels);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const modelBusy = useModelStore((state) => state.modelBusy);
  const setModelBusy = useModelStore((state) => state.setModelBusy);
  const modelProgress = useModelStore((state) => state.modelProgress);
  const liveLlamacppStatus = useModelStore((state) => state.llamacppStatus);
  const setModelProgress = useModelStore((state) => state.setModelProgress);
  const setLlamacppStatus = useModelStore((state) => state.setLlamacppStatus);
  const [executionTasks, setExecutionTasks] = useState<ExecutionTaskSummary[]>([]);
  // What `executionTasks` currently holds, as text. Every poll response is a
  // freshly parsed array, so comparing references can never say "unchanged";
  // this can, and lets an idle poll skip the state write (and the whole-shell
  // render that comes with it). "[]" matches the initial state above.
  const executionTasksSignatureRef = useRef("[]");
  // The saved preference lives only in the settings store; the theme is
  // derived from it (and painted by useAppliedTheme), never copied into state.
  const theme = settings?.appearance?.theme ?? DEFAULT_THEME_PREFERENCE;
  useAppliedTheme(settings ? theme : null);
  const chatsRef = useRef(chats);
  const executionTaskRefreshRef = useRef<Promise<void> | null>(null);
  // These guards cover requests whose results are deliberately loaded out of
  // band. A response can outlive both the load that started it and this
  // authenticated workspace instance (for example when a 401 returns the
  // app to onboarding). Keep each mutable inventory independent so a group
  // mutation does not invalidate an unrelated model refresh and vice versa.
  const mountedRef = useRef(false);
  const workspaceLoadGenerationRef = useRef(0);
  const groupLoadGenerationRef = useRef(0);
  const modelGenerationRef = useRef(0);
  const memoryGenerationRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      workspaceLoadGenerationRef.current += 1;
      groupLoadGenerationRef.current += 1;
      modelGenerationRef.current += 1;
      memoryGenerationRef.current += 1;
      // The model job itself is durable on the backend, but its UI ownership
      // ends with this authenticated workspace. Do not strand a busy flag in
      // the process-wide store after logout or a remount.
      setModelBusy(false);
      setModelProgress(null);
    };
  }, [setModelBusy, setModelProgress]);

  useEffect(() => {
    chatsRef.current = chats;
  }, [chats]);

  const loadWorkspace = useCallback(async () => {
    const loadGeneration = ++workspaceLoadGenerationRef.current;
    const groupGeneration = ++groupLoadGenerationRef.current;
    const modelGeneration = ++modelGenerationRef.current;
    const isCurrentLoad = () => mountedRef.current && workspaceLoadGenerationRef.current === loadGeneration;
    const isCurrentGroupLoad = () => isCurrentLoad() && groupLoadGenerationRef.current === groupGeneration;
    const isCurrentModelLoad = () => isCurrentLoad() && modelGenerationRef.current === modelGeneration;
    setLoading(true);
    setLoadError(null);
    try {
      // Memories are not here on purpose: they render only inside Settings, so
      // an unreadable memory store must not stand between the user and chat.
      // They load when the settings route opens.
      const [systemResponse, chatResponse, settingsResponse] = await Promise.all([
        api.system(),
        api.chats(),
        api.settings(),
      ]);
      if (!isCurrentLoad()) return;
      setSystem(systemResponse);
      setLlamacppStatus(systemResponse.llamacpp ?? null);
      setChats(chatResponse);
      // Groups are organisation on top of the chats, so they load out of band
      // like the model inventory does: if the endpoint is unavailable the
      // library still opens with every chat present, just ungrouped. Blocking
      // the whole workspace on filing metadata would be the wrong trade.
      void api.chatGroups()
        .then((nextGroups) => {
          if (isCurrentGroupLoad()) setGroups(nextGroups);
        })
        .catch(() => {
          if (isCurrentGroupLoad()) setGroups([]);
        });
      setSettings(settingsResponse.settings);
      if (isCurrentModelLoad()) setModels(UNAVAILABLE_MODELS);
      void api.models()
        .then((nextModels) => {
          if (isCurrentModelLoad()) setModels(nextModels);
        })
        .catch(() => {
          if (isCurrentModelLoad()) setModels(UNAVAILABLE_MODELS);
        });
    } catch (error) {
      // No 401 branch anywhere in this file: the client renews an expired
      // session in place, and only when it cannot does it clear the session
      // and tell the app's listener, which owns the way back to onboarding.
      if (isCurrentLoad()) setLoadError(describeApiError(error, "Could not load the local workspace."));
    } finally {
      if (isCurrentLoad()) setLoading(false);
    }
  }, [api, setChats, setGroups, setModels, setSettings, setLlamacppStatus]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadWorkspace(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadWorkspace]);

  // Memories are read for the Settings screen only, so they load when that
  // route opens, with their own error state, rather than gating the workspace.
  // Opening Settings again refreshes them: the assistant can add memories from
  // a chat, and saving an edited copy of a stale list would erase those.
  const loadMemories = useCallback(async () => {
    const generation = ++memoryGenerationRef.current;
    const isCurrentLoad = () => mountedRef.current && memoryGenerationRef.current === generation;
    // Keep showing the last list during a refresh; only a first load, or a
    // retry after a failure, has nothing to show.
    setMemoryLoad((current) => (current.status === "ready" ? current : { status: "loading" }));
    try {
      const response = await api.memories();
      if (!isCurrentLoad()) return;
      setMemos(response.memos);
      setMemoryLoad({ status: "ready" });
    } catch (error) {
      if (isCurrentLoad()) {
        setMemoryLoad({ status: "error", message: apiMessage(error, "Could not load your saved memories.") });
      }
    }
  }, [api]);

  // A mutation's response is the authoritative list. Invalidate any load still
  // in flight so it cannot overwrite it with an older read.
  const applyMemories = (nextMemos: string[]) => {
    ++memoryGenerationRef.current;
    setMemos(nextMemos);
    setMemoryLoad({ status: "ready" });
  };

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
        const signature = JSON.stringify(response.tasks);
        if (signature === executionTasksSignatureRef.current) return;
        executionTasksSignatureRef.current = signature;
        setExecutionTasks(response.tasks);
      } catch {
        // A failed poll keeps the last list and the next tick retries.
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
  }, [api]);

  // A second is the right cadence while something is actually running or
  // waiting on approval. With nothing in flight it was still a SQLite query
  // every second for the life of the app, so back off -- slowly enough that a
  // task started elsewhere still appears promptly.
  const hasActiveExecutionTask = executionTasks.some(
    (task) => !EXECUTION_TERMINAL_STATUSES.has(task.status),
  );
  useVisiblePolling(
    refreshExecutionTasks,
    hasActiveExecutionTask ? 1000 : 5000,
    Boolean(system?.execution_preview_available),
  );

  // Only poll the local llama.cpp runtime state while a GGUF model is
  // actually selected -- Ollama users never spend cycles on this. A GGUF
  // model can take a while to download/start on first use, and this is
  // what lets the composer show live "loaded" / "starting" state instead
  // of only ever reflecting whatever was true at the last full page load.
  const selectedModelIsGGUF = isGGUFModel(settings?.models?.chat ?? null);
  const refreshLlamacppStatus = useCallback(async () => {
    try {
      const response = await api.system();
      const next = response.llamacpp ?? null;
      // Every response parses to a new object, and the store notifies on any
      // new reference, so an unchanged status would re-render the shell every
      // two seconds for as long as a GGUF model is selected.
      if (JSON.stringify(next) !== JSON.stringify(useModelStore.getState().llamacppStatus)) {
        setLlamacppStatus(next);
      }
    } catch {
      // Keep the last known status; the next tick retries.
    }
  }, [api, setLlamacppStatus]);
  useVisiblePolling(refreshLlamacppStatus, 2000, selectedModelIsGGUF);

  const visibleExecutionTasks = system?.execution_preview_available
    ? executionTasks.filter((task) => shouldShowExecutionTask(task, system.started_at))
    : [];

  const cancelExecution = async (jobId: string) => {
    try {
      await api.cancelExecution(jobId);
      await refreshExecutionTasks();
    } catch (error) {
      notify(apiMessage(error, "Could not stop the background task."), "error");
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
      notify(apiMessage(error, "Could not record the approval decision."), "error");
    }
  };

  const loadCodeSource = (jobId: string) => api.executionSource(jobId);

  const renameChat = async (id: string, title: string): Promise<boolean> => {
    try {
      const chat = await api.renameChat(id, title);
      setChats((current) => current.map((item) => item.id === id ? { ...item, title: chat.title, timestamp: chat.timestamp } : item));
      notify("Chat renamed.", "success");
      return true;
    } catch (error) { notify(apiMessage(error, "Could not rename chat."), "error"); return false; }
  };

  const downloadExecutionArtifact = async (artifact: ExecutionArtifactResult) => {
    try {
      const response = await api.downloadExecutionArtifact(artifact.artifact_id);
      const objectUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `cortex-result.${artifactFileExtension(artifact.mime_type)}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (error) {
      notify(apiMessage(error, "Could not download the execution artifact."), "error");
    }
  };

  const deleteChat = async (id: string): Promise<boolean> => {
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
      return true;
    } catch (error) { notify(apiMessage(error, "Could not delete chat."), "error"); return false; }
  };

  const createGroup = async (name: string): Promise<boolean> => {
    try {
      const group = await api.createChatGroup(name);
      if (mountedRef.current) {
        // Invalidate the initial snapshot only once the mutation succeeded.
        // A failed mutation must still allow that snapshot to populate the
        // library, while a late successful snapshot must not erase this row.
        ++groupLoadGenerationRef.current;
        upsertGroup(group);
      }
      notify("Group created.", "success");
      return true;
    } catch (error) { notify(apiMessage(error, "Could not create group."), "error"); return false; }
  };

  const renameGroup = async (groupId: string, name: string): Promise<boolean> => {
    try {
      const group = await api.updateChatGroup(groupId, { name });
      if (mountedRef.current) {
        ++groupLoadGenerationRef.current;
        upsertGroup(group);
      }
      return true;
    } catch (error) { notify(apiMessage(error, "Could not rename group."), "error"); return false; }
  };

  const deleteGroup = async (groupId: string): Promise<boolean> => {
    try {
      await api.deleteChatGroup(groupId);
      // Local mirror of the server rule: the group goes, its chats stay and
      // reappear in the ungrouped list.
      if (mountedRef.current) {
        ++groupLoadGenerationRef.current;
        removeGroup(groupId);
      }
      notify("Group deleted. Its chats moved back to the main list.", "success");
      return true;
    } catch (error) { notify(apiMessage(error, "Could not delete group."), "error"); return false; }
  };

  const toggleGroup = (groupId: string, collapsed: boolean) => {
    const previous = useChatStore.getState().groups.find((group) => group.id === groupId);
    if (!previous) return;
    ++groupLoadGenerationRef.current;
    // Optimistic: collapsing is a high-frequency, zero-risk interaction and
    // must feel instant. The persisted value is only a preference, so a
    // failed write rolls the row back and stays quiet.
    upsertGroup({ ...previous, collapsed });
    void api.updateChatGroup(groupId, { collapsed }).catch(() => {
      if (!mountedRef.current) return;
      // Roll back only the failed preference field. A later rename or toggle
      // may have legitimately changed other fields while this request was in
      // flight, and must not be clobbered by the old whole-row snapshot.
      setGroups((current) => current.map((group) => (
        group.id === groupId && group.collapsed === collapsed
          ? { ...group, collapsed: previous.collapsed }
          : group
      )));
    });
  };

  const moveChat = (threadId: string, groupId: string | null) => {
    const previous = useChatStore.getState().chats.find((chat) => chat.id === threadId)?.group_id ?? null;
    setChatGroup(threadId, groupId);
    void api.moveChatToGroup(threadId, groupId).catch((error) => {
      // Only undo our own optimistic write. A second move issued while this
      // one was in flight may have already landed, and rolling back blindly
      // would drag the chat out of the group the user just watched it reach.
      const current = useChatStore.getState().chats.find((chat) => chat.id === threadId);
      if (current !== undefined && current.group_id === groupId) {
        setChatGroup(threadId, previous);
      }
      notify(apiMessage(error, "Could not move chat."), "error");
    });
  };

  /** Resolves to the saved document, or null when nothing was saved. */
  const saveSettings = async (next: CortexSettings): Promise<CortexSettings | null> => {
    setSaving(true);
    try {
      const response = await api.updateSettings({ settings: next, expected_revision: next.revision });
      setSettings(response.settings);
      notify("Settings saved.", "success");
      return response.settings;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // The server's compare-and-swap rejected a concurrent settings write.
        // Refresh the authoritative document so a mounted SettingsPanel can
        // merge its still-local edits against the newer revision before the
        // user retries, instead of leaving a stale snapshot in the store.
        try {
          const latest = await api.settings();
          setSettings(latest.settings);
          notify("Settings changed elsewhere. Review the latest values and save again.", "error");
        } catch (refreshError) {
          notify(apiMessage(refreshError, "Could not refresh settings after the conflict."), "error");
        }
      } else {
        notify(apiMessage(error, "Could not save settings."), "error");
      }
      return null;
    }
    finally { setSaving(false); }
  };

  const addMemory = async (memo: string) => {
    setMemoryBusy(true);
    try {
      const response = await api.addMemory(memo);
      applyMemories(response.memos);
      notify("Memory saved.", "success");
    } catch (error) {
      notify(apiMessage(error, "Could not save memory."), "error");
      throw error;
    }
    finally { setMemoryBusy(false); }
  };

  const clearMemory = async () => {
    setMemoryBusy(true);
    try {
      const response: MemoryResponse = await api.clearMemories();
      applyMemories(response.memos);
      notify("Permanent memories cleared.", "success");
    } catch (error) {
      notify(apiMessage(error, "Could not clear memories."), "error");
      throw error;
    }
    finally { setMemoryBusy(false); }
  };

  const replaceMemory = async (next: string[]) => {
    setMemoryBusy(true);
    try {
      const response = await api.replaceMemories(next);
      applyMemories(response.memos);
      notify("Memory changes saved.", "success");
    } catch (error) {
      notify(apiMessage(error, "Could not save memory changes."), "error");
      throw error;
    }
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
    const generation = ++modelGenerationRef.current;
    const isCurrentModelJob = () => mountedRef.current && modelGenerationRef.current === generation;
    setModelBusy(true);
    setModelProgress({ model, status: "Starting...", percent: null });
    let completedData: Record<string, unknown> | null = null;
    let failureMessage: string | null = null;
    try {
      const terminalEvent = await api.streamJob(accepted.job_id, (event) => {
        if (mountedRef.current && modelGenerationRef.current === generation) updateModelProgress(event, setModelProgress);
        if (event.kind === "completed") completedData = event.data ?? null;
        if (event.kind === "error") {
          const message = event.data?.message;
          failureMessage = typeof message === "string" && message ? message : "Model operation failed.";
        }
      });

      // A clean SSE close carries the terminal event back from streamJob. If
      // the connection ends before that event, reconcile against the durable
      // job snapshot before treating the operation as complete.
      let terminalStatus: JobStatusResponse["status"] | null = terminalEvent ? terminalEvent.status : null;
      if (!terminalEvent) {
        const snapshot = await api.jobStatus(accepted.job_id);
        terminalStatus = snapshot.status;
        if (snapshot.status === "succeeded") completedData = snapshot.result ?? null;
        if (snapshot.status === "failed" || snapshot.status === "cancelled") {
          failureMessage = snapshot.error ?? null;
        }
      }

      if (terminalStatus === "failed" || terminalStatus === "cancelled") {
        if (isCurrentModelJob()) {
          notify(
            failureMessage
              ?? (terminalStatus === "cancelled" ? "Model operation was cancelled." : "Model operation failed."),
            "error",
          );
        }
        return null;
      }
      if (terminalStatus !== "succeeded") {
        // Do not refresh inventory, announce success, or imply that a GGUF
        // download produced a selectable file while the worker may continue.
        if (isCurrentModelJob()) {
          setModelProgress({ model, status: "Model operation is still running; completion was not confirmed.", percent: null });
          notify("Model operation is still running; completion was not confirmed.", "error");
        }
        return null;
      }

      const refreshedModels = await api.models();
      if (!isCurrentModelJob()) return completedData;
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
      if (isCurrentModelJob()) notify(apiMessage(error, "Model operation failed."), "error");
      return null;
    } finally {
      if (isCurrentModelJob()) setModelBusy(false);
    }
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
  // The initial system response seeds the store, while polling updates it as
  // the managed runtime starts/stops. Settings must consume that live value
  // rather than the immutable workspace snapshot.
  const llamacppStatus = liveLlamacppStatus ?? system.llamacpp ?? DEFAULT_LLAMACPP_STATUS;
  // Resolve readiness against the runtime selected by the user. Ollama's
  // connection state must not affect a GGUF selection (or vice versa).
  const runtimeAvailability = resolveRuntimeAvailability({
    selectedModel,
    selectedModelAvailable,
    ollamaConnected: models.connection?.success ?? true,
    ollamaMessage: models.connection?.message,
    llamacppStatus,
  });
  const routeChatId = route.kind === "chat" ? route.threadId : null;
  const openSettings = () => {
    // Settings can be opened from either the shell header or the command
    // palette. Capture the route at the point of entry so Close returns to
    // the chat that was actually visible, including a newly-created thread.
    const currentRoute = parseAppRoute(window.location.pathname);
    if (currentRoute.kind === "chat") setSettingsReturnChatId(currentRoute.threadId);
    navigate("/settings");
  };
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    void saveSettings({ ...settings, appearance: { ...settings.appearance, theme: next } });
  };

  return (
    <>
      <AppShell chats={chats} activeChatId={routeChatId} modelConnection={models.connection} theme={theme} executionTasks={visibleExecutionTasks} onCancelExecution={cancelExecution} onDecideExecutionApproval={decideExecutionApproval} onLoadCodeSource={loadCodeSource} onDownloadArtifact={downloadExecutionArtifact} onOpenSettings={openSettings} onRenameChat={renameChat} onDeleteChat={deleteChat} groups={groups} onCreateGroup={createGroup} onRenameGroup={renameGroup} onDeleteGroup={deleteGroup} onToggleGroup={toggleGroup} onMoveChat={moveChat}>
        <Suspense fallback={<div className="loading-state" role="status" aria-live="polite"><span className="loading-spinner" />Loading workspace...</div>}>
          {route.kind === "settings"
            ? <SettingsRoute activeChatId={settingsReturnChatId} settings={settings} memos={memos} memoryLoad={memoryLoad} onLoadMemories={loadMemories} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onReplaceMemory={replaceMemory} onClearMemory={clearMemory} models={models} modelBusy={modelBusy} modelProgress={modelProgress} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onCheckModels={checkModels} onPullModel={pullModel} llamacppStatus={llamacppStatus} onDownloadGGUF={downloadGGUFModel} />
            : <ChatRoute threadId={routeChatId} api={api} runtimeReady={runtimeAvailability.ready} runtimeMessage={runtimeAvailability.message} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={modelBusy || saving} onSelectModel={chooseLocalModel} onRescanModels={checkModels} onChatChanged={upsertChatSummary} onForked={upsertChatSummary} onClearMemory={clearMemory} onSessionExpired={onSessionExpired} />}
        </Suspense>
      </AppShell>
      <CommandPalette
        chats={chats}
        localModels={localModels}
        selectedModel={selectedModel}
        onNewChat={() => navigate("/chat/new")}
        onOpenSettings={openSettings}
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

function ChatRoute({ threadId, api, runtimeReady, runtimeMessage, localModels, selectedModel, selectedModelSupportsVision, modelBusy, onSelectModel, onRescanModels, onChatChanged, onForked, onClearMemory, onSessionExpired }: { threadId: string | null; api: CortexApi; runtimeReady: boolean; runtimeMessage: string | null; localModels: readonly string[]; selectedModel: string | null; selectedModelSupportsVision: boolean | null; modelBusy: boolean; onSelectModel: (model: string) => Promise<boolean>; onRescanModels: () => Promise<void>; onChatChanged: (chat: ChatResponse) => void; onForked: (chat: ChatResponse) => void; onClearMemory: () => Promise<void>; onSessionExpired: () => void }) {
  const navigate = useNavigate();
  return <ChatPage api={api} threadId={threadId} runtimeReady={runtimeReady} runtimeMessage={runtimeMessage} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={modelBusy} onSelectModel={onSelectModel} onRescanModels={onRescanModels} onThreadCreated={(id) => navigate(chatPath(id), { replace: true })} onChatChanged={onChatChanged} onForked={(chat) => { onForked(chat); navigate(chatPath(chat.id)); }} onClearMemory={onClearMemory} onSessionExpired={onSessionExpired} />;
}

function SettingsRoute({ activeChatId, onLoadMemories, ...props }: Omit<SettingsPanelProps, "onClose" | "onRetryMemory"> & { activeChatId: string | null; onLoadMemories: () => Promise<void> }) {
  const navigate = useNavigate();
  useEffect(() => {
    void onLoadMemories();
  }, [onLoadMemories]);
  return <SettingsPanel {...props} onRetryMemory={() => void onLoadMemories()} onClose={() => navigate(activeChatId ? chatPath(activeChatId) : "/chat/new")} />;
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
  return describeApiError(error, fallback);
}

function artifactFileExtension(mimeType: string): string {
  return ({
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "text/plain": "txt",
    "application/json": "json",
  } as Record<string, string>)[mimeType] ?? "bin";
}
