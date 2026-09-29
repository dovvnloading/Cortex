import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ChatResponse, CortexSettings, ExecutionApprovalDecisionRequest, ExecutionTaskSummary, JobAccepted, LlamaCppRuntimeStatus, MemoryResponse, ModelDownloadRequest, ModelResponse, SystemResponse } from "../../../contracts/cortex-api";
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
const ChatPage = lazyRoute(loadChatPage);
import { GenerationStreamHost } from "../features/chat/GenerationStreamHost";
import { Onboarding } from "../features/shell/Onboarding";
const SettingsPanel = lazyRoute<SettingsPanelProps>(() => import("../features/settings/SettingsPanel").then(({ SettingsPanel: component }) => ({ default: component })));
import type { SettingsPanelProps } from "../features/settings/SettingsPanel";
import type { MemoryLoadState } from "../features/settings/MemoryPanel";
import { blockStrayFileDrops } from "../lib/attachments";
import { discardComposerDraft, pruneComposerDrafts } from "../lib/composerDraft";
import { displayModelName, isGGUFModel, localModelNames } from "../lib/localModels";
import { ModelJobCancelledError } from "../lib/modelJobs";
import { chatPath, navigate, parseAppRoute, useNavigate, usePathname } from "../lib/navigation";
import { applyStoredTheme, DEFAULT_THEME_PREFERENCE } from "../lib/theme";
import { useAppliedTheme } from "../hooks/useAppliedTheme";
import { useModelJobs } from "../hooks/useModelJobs";
import { useVisiblePolling } from "../hooks/useVisiblePolling";
import { useChatStore } from "../stores/useChatStore";
import { useModelStore } from "../stores/useModelStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { RouteBoundary } from "./ErrorBoundary";
import { lazyRoute } from "./lazyRoute";
import { useToast } from "./ToastProvider";
import { resolveRuntimeAvailability } from "./runtimeAvailability";

type Props = { api?: CortexApi };

/**
 * How long "Chat deleted" offers Undo. The request is sent when this window
 * closes, so it is also how long a deleted chat still exists on the backend.
 */
const CHAT_DELETE_UNDO_MS = 6000;

/** Statuses an execution task never leaves, so nothing more will change. */
const EXECUTION_TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

/**
 * What the model inventory shows when the request for it failed. Distinct from
 * "not answered yet", which is a null inventory in the store: a placeholder that
 * looked like this one on every launch reported a fault that was not there.
 */
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

/** Shown only while the first inventory request is in flight: empty, and no connection verdict either way. */
const LOADING_MODELS: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  missing_models: [],
  optional_missing_models: [],
  models: [],
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
  const modelProgress = useModelStore((state) => state.modelProgress);
  const liveLlamacppStatus = useModelStore((state) => state.llamacppStatus);
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
  // A chat that was just deleted is hidden here at once but only removed from
  // the backend when its Undo window closes (see `deleteChat`). Everything that
  // lists or picks a chat reads `visibleChats`, so a hidden chat stays hidden
  // even if the store's list is refilled from the server or a finishing
  // generation upserts it, and Undo puts it back exactly as it was.
  const pendingChatDeletesRef = useRef(new Set<string>());
  const [pendingChatDeleteIds, setPendingChatDeleteIds] = useState<ReadonlySet<string>>(() => new Set());
  const visibleChats = useMemo(
    () => chats.filter((chat) => !pendingChatDeleteIds.has(chat.id)),
    [chats, pendingChatDeleteIds],
  );
  const setChatDeletePending = useCallback((id: string, pending: boolean) => {
    if (pending) pendingChatDeletesRef.current.add(id);
    else pendingChatDeletesRef.current.delete(id);
    setPendingChatDeleteIds(new Set(pendingChatDeletesRef.current));
  }, []);
  const chatsRef = useRef(visibleChats);
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
    };
  }, []);

  useEffect(() => {
    chatsRef.current = visibleChats;
  }, [visibleChats]);

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
      // `chatResponse` is the whole list, so any other saved draft belongs to a chat that is gone.
      pruneComposerDrafts(chatResponse.map((chat) => chat.id));
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
      // Nothing is known about the inventory until it answers (see LOADING_MODELS).
      if (isCurrentModelLoad()) setModels(null);
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

  // Deleting hides the chat at once and asks the backend only when the Undo
  // window closes. Closing the app inside that window loses nothing: the
  // request was never sent, so the chat is simply still there next launch.
  const deleteChat = async (id: string): Promise<boolean> => {
    if (pendingChatDeletesRef.current.has(id)) return true;
    const fallbackChatId = chatsRef.current.find((chat) => chat.id !== id)?.id ?? null;
    const fallbackPath = fallbackChatId ? chatPath(fallbackChatId) : "/chat/new";
    const currentRoute = parseAppRoute(window.location.pathname);
    const wasOpen = currentRoute.kind === "chat" && currentRoute.threadId === id;
    setChatDeletePending(id, true);
    setSettingsReturnChatId((current) => current === id ? fallbackChatId : current);
    if (wasOpen) navigate(fallbackPath, { replace: true });

    const commit = async () => {
      try {
        await api.deleteChat(id);
        discardComposerDraft(id);
        // Take the row out of the list before it is un-hidden, so it cannot flash back.
        setChats((current) => current.filter((chat) => chat.id !== id));
        setChatDeletePending(id, false);
      } catch (error) {
        setChatDeletePending(id, false);
        notify(apiMessage(error, "Could not delete chat."), "error");
      }
    };
    const undo = () => {
      setChatDeletePending(id, false);
      // Only follow the chat back if the person is still where deleting it left them.
      if (wasOpen && window.location.pathname === fallbackPath) navigate(chatPath(id), { replace: true });
      notify("Chat restored.", "success");
    };
    notify("Chat deleted.", "success", {
      action: { label: "Undo", onAction: undo },
      durationMs: CHAT_DELETE_UNDO_MS,
      onClose: () => void commit(),
    });
    return true;
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

  // A model job is followed by `useModelJobs`, which keeps the job in the
  // model store (and session storage) so a remount or reload picks it up
  // again. A finished job's fresh inventory supersedes any initial load still
  // in flight, and a download that finishes with nothing waiting on it (one
  // picked up again after a reload) is selected here just like one started
  // from Settings.
  const modelJobs = useModelJobs({
    api,
    notify,
    onModels: (refreshed) => {
      modelGenerationRef.current += 1;
      setModels(refreshed);
    },
    onDownloaded: (filename) => finishGGUFDownload(filename),
  });

  const listHuggingFaceFiles = (repoId: string, signal: AbortSignal) => api.listHuggingFaceGGUFFiles(repoId, { signal });

  const checkModels = async () => {
    try { await modelJobs.run(await api.checkModels()); }
    catch (error) { notify(apiMessage(error, "Could not check Ollama models."), "error"); }
  };

  const pullModel = async (model: string) => {
    try { await modelJobs.run(await api.pullModel(model), model); }
    catch (error) { notify(apiMessage(error, "Could not start the model pull."), "error"); }
  };

  const finishGGUFDownload = async (filename: string) => {
    const selected = await chooseLocalModel(`gguf:${filename}`);
    if (!selected) {
      notify(`${filename} downloaded. Select it from the model menu to start chatting.`, "success");
    }
  };

  const downloadGGUFModel = async (request: ModelDownloadRequest) => {
    const label = request.source === "huggingface" ? request.filename ?? "GGUF model" : "GGUF model";
    let accepted: JobAccepted;
    try {
      accepted = await api.downloadGGUFModel(request);
    } catch (error) {
      const message = apiMessage(error, "Could not start the model download.");
      notify(message, "error");
      throw new Error(message, { cause: error });
    }
    const result = await modelJobs.run(accepted, label, { checkOllamaConnection: false, notifyOnSuccess: false });
    // The workspace closed first; the download carries on and is picked up
    // again (and selected) by the next one.
    if (result.detached) return;
    if (result.cancelled) throw new ModelJobCancelledError();
    const filename = result.succeeded && typeof result.data?.filename === "string" ? result.data.filename : null;
    if (!filename) {
      // The job's own toast already gave the specific failure reason; the form
      // shows the same sentence beside its button.
      throw new Error(result.failure ?? "Model download failed.");
    }
    await finishGGUFDownload(filename);
  };

  const unloadLocalModel = async () => {
    try {
      // The answer is the new status, so the panel reflects it at once rather
      // than at the next poll (which only runs while a GGUF model is selected).
      setLlamacppStatus(await api.unloadLlamaCpp());
      notify("The local model was unloaded. It loads again when you send a message.", "success");
    } catch (error) {
      notify(apiMessage(error, "Could not unload the local model."), "error");
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
  if (loadError || !system || !settings) {
    return <main className="fatal-state"><h1>Workspace unavailable</h1><p>{loadError ?? "Cortex returned an incomplete workspace."}</p><button className="button button-primary" onClick={() => void loadWorkspace()}>Retry</button></main>;
  }

  // The inventory is fetched after the workspace opens, so for a moment it is
  // unknown rather than empty: an empty stand-in with no connection report.
  const inventoryLoading = models === null;
  const inventory = models ?? LOADING_MODELS;
  const localModels = localModelNames(inventory);
  const hasLocalInventory = Array.isArray(inventory.installed_models) || Array.isArray(inventory.models);
  const selectedModel = settings.models?.chat?.trim() || null;
  const selectedModelSupportsVision = inventory.models?.find((model) => model.name === selectedModel)?.supports_vision ?? null;
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
    inventoryLoading,
    ollamaConnected: inventory.connection?.success ?? true,
    ollamaMessage: inventory.connection?.message,
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
      {/* Above the route switch, so a generation keeps streaming into the store while Settings is open. */}
      <GenerationStreamHost api={api} onSessionExpired={onSessionExpired} />
      <AppShell chats={visibleChats} activeChatId={routeChatId} modelConnection={inventory.connection} theme={theme} executionTasks={visibleExecutionTasks} onCancelExecution={cancelExecution} onDecideExecutionApproval={decideExecutionApproval} onLoadCodeSource={loadCodeSource} onDownloadArtifact={downloadExecutionArtifact} onOpenSettings={openSettings} onRenameChat={renameChat} onDeleteChat={deleteChat} groups={groups} onCreateGroup={createGroup} onRenameGroup={renameGroup} onDeleteGroup={deleteGroup} onToggleGroup={toggleGroup} onMoveChat={moveChat}>
        <Suspense fallback={<div className="loading-state" role="status" aria-live="polite"><span className="loading-spinner" />Loading workspace...</div>}>
          {route.kind === "settings"
            ? <RouteBoundary key="settings" name="Settings" scope="settings" resetKey={pathname} onRetry={SettingsPanel.reload}><SettingsRoute activeChatId={settingsReturnChatId} settings={settings} memos={memos} memoryLoad={memoryLoad} onLoadMemories={loadMemories} saving={saving} memoryBusy={memoryBusy} onSave={saveSettings} onAddMemory={addMemory} onReplaceMemory={replaceMemory} onClearMemory={clearMemory} models={inventory} modelBusy={modelBusy} modelProgress={modelProgress} onCancelModelJob={modelJobs.cancel} setupUrl={system.ollama_setup_url ?? "https://ollama.com/download"} onCheckModels={checkModels} onPullModel={pullModel} llamacppStatus={llamacppStatus} onDownloadGGUF={downloadGGUFModel} onListHuggingFaceFiles={listHuggingFaceFiles} onUnloadModel={unloadLocalModel} /></RouteBoundary>
            : <RouteBoundary key="chat" name="Chat" scope="chat" resetKey={pathname} onRetry={ChatPage.reload}><ChatRoute threadId={routeChatId} api={api} runtimeReady={runtimeAvailability.ready} runtimeMessage={runtimeAvailability.message} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={saving} onSelectModel={chooseLocalModel} onRescanModels={checkModels} onForked={upsertChatSummary} onClearMemory={clearMemory} /></RouteBoundary>}
        </Suspense>
      </AppShell>
      <CommandPalette
        chats={visibleChats}
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

function ChatRoute({ threadId, api, runtimeReady, runtimeMessage, localModels, selectedModel, selectedModelSupportsVision, modelBusy, onSelectModel, onRescanModels, onForked, onClearMemory }: { threadId: string | null; api: CortexApi; runtimeReady: boolean; runtimeMessage: string | null; localModels: readonly string[]; selectedModel: string | null; selectedModelSupportsVision: boolean | null; modelBusy: boolean; onSelectModel: (model: string) => Promise<boolean>; onRescanModels: () => Promise<void>; onForked: (chat: ChatResponse) => void; onClearMemory: () => Promise<void> }) {
  const navigate = useNavigate();
  return <ChatPage api={api} threadId={threadId} runtimeReady={runtimeReady} runtimeMessage={runtimeMessage} localModels={localModels} selectedModel={selectedModel} selectedModelSupportsVision={selectedModelSupportsVision} modelBusy={modelBusy} onSelectModel={onSelectModel} onRescanModels={onRescanModels} onThreadCreated={(id) => navigate(chatPath(id), { replace: true })} onForked={(chat) => { onForked(chat); navigate(chatPath(chat.id)); }} onClearMemory={onClearMemory} />;
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
