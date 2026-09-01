import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatAttachment, ChatMessage, ChatResponse, GenerationOptionsOverride } from "../../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../../api/client";
import { displayChatTitle } from "../../lib/chatTitle";
import { composerAttachmentKey, composerDraftKey, readComposerAttachments, readComposerDraft, writeComposerAttachments, writeComposerDraft } from "../../lib/composerDraft";
import { humanizeGenerationStatus } from "../../lib/generationStatus";
import { readActiveJob, useGenerationStream, type PersistedJob } from "../../hooks/useGenerationStream";
import { NEW_THREAD_OPTIONS_KEY, useChatStore } from "../../stores/useChatStore";
import { useSettingsStore } from "../../stores/useSettingsStore";
import { useUiStore } from "../../stores/useUiStore";
import { MessageComposer, type ComposerPhase } from "./MessageComposer";
import { MessageList, type MessageListHandle } from "./MessageList";
import { SafeMarkdown } from "../markdown/SafeMarkdown";

const DEFAULT_GENERATION_SETTINGS = {
  temperature: 0.7,
  top_p: 0.9,
  top_k: 40,
  repeat_penalty: 1.1,
  num_ctx: 8192,
  seed: -1,
  system_instructions: "",
};

type Props = {
  api: CortexApi;
  threadId: string | null;
  runtimeReady: boolean;
  runtimeMessage: string | null;
  localModels: readonly string[];
  selectedModel: string | null;
  selectedModelSupportsVision?: boolean | null;
  modelBusy: boolean;
  onSelectModel: (model: string) => Promise<boolean>;
  onRescanModels: () => Promise<void>;
  onThreadCreated: (threadId: string) => void;
  onChatChanged: (chat: ChatResponse) => void;
  onForked: (chat: ChatResponse) => void;
  onClearMemory?: () => Promise<void>;
  onSessionExpired: () => void;
};

type ScopedError = {
  message: string;
  threadId: string | null;
};

type ChatLoadState = {
  threadId: string | null;
  loading: boolean;
  error: string | null;
};

type StartedGeneration = {
  threadId: string;
};

type AttachmentDraftTarget = {
  scope: string;
  threadId: string | null;
};

type PendingAdmission = {
  requestId: string;
  operation: "generate" | "regenerate";
  threadId: string | null;
  baseRevision?: number;
  options?: GenerationOptionsOverride;
  messageId?: string;
};

const MAX_CHAT_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const MAX_CHAT_ATTACHMENT_TOTAL_BYTES = 24 * 1024 * 1024;
const MAX_CHAT_ATTACHMENTS = 8;

export function ChatPage({
  api,
  threadId,
  runtimeReady,
  runtimeMessage,
  localModels,
  selectedModel,
  selectedModelSupportsVision = null,
  modelBusy,
  onSelectModel,
  onRescanModels,
  onThreadCreated,
  onChatChanged,
  onForked,
  onClearMemory,
  onSessionExpired,
}: Props) {
  const generation = useChatStore((state) => state.generation);
  const generationOptionsByThread = useChatStore((state) => state.generationOptionsByThread);
  const setThreadOptions = useChatStore((state) => state.setThreadOptions);
  const generationDefaults = useSettingsStore((state) => state.settings?.generation) ?? DEFAULT_GENERATION_SETTINGS;
  const { start, consume, stop } = useGenerationStream(api, onSessionExpired);
  const [chat, setChat] = useState<ChatResponse | null>(null);
  const [resolvedThreadId, setResolvedThreadId] = useState<string | null>(threadId);
  const [drafts, setDrafts] = useState<Record<string, string>>(() => ({
    [composerDraftKey(threadId)]: readComposerDraft(threadId),
  }));
  const [attachmentDrafts, setAttachmentDrafts] = useState<Record<string, ChatAttachment[]>>(() => ({
    [composerAttachmentKey(threadId)]: readComposerAttachments(threadId),
  }));
  const [lastPrompt, setLastPrompt] = useState("");
  const [lastAttachments, setLastAttachments] = useState<ChatAttachment[]>([]);
  const [chatLoad, setChatLoad] = useState<ChatLoadState>({
    threadId,
    loading: true,
    error: null,
  });
  const [generationError, setGenerationError] = useState<ScopedError | null>(null);
  const [starting, setStarting] = useState(false);
  const [forkingMessage, setForkingMessage] = useState<string | null>(null);
  const [attachmentsBusy, setAttachmentsBusy] = useState(false);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const startingRef = useRef(false);
  const stoppingRef = useRef(false);
  const messageListRef = useRef<MessageListHandle>(null);
  const isNearTranscriptEnd = useRef(true);
  const viewThreadIdRef = useRef<string | null>(threadId);
  const chatRequestVersionsRef = useRef(new Map<string | null, number>());
  const initialMountRef = useRef(true);
  const draftsRef = useRef(drafts);
  const attachmentDraftsRef = useRef(attachmentDrafts);
  const attachmentDraftTargetsRef = useRef(new Set<AttachmentDraftTarget>());
  // A POST can be admitted before its response reaches the browser. Keep its
  // idempotency key across that ambiguous failure so Retry can replay the
  // admission instead of creating a second job (or a second new-chat thread).
  // A deliberate submit does not pass this key and therefore starts a new
  // user turn with a fresh request id.
  const pendingAdmissionRef = useRef<PendingAdmission | null>(null);
  const handledClearRequestsRef = useRef(new Set<string>());

  const reportGenerationFailure = useCallback((failedThreadId: string, message: string) => {
    setGenerationError({ threadId: failedThreadId, message });
  }, []);

  const loadChat = useCallback(async ({ preserveCurrent = false }: { preserveCurrent?: boolean } = {}) => {
    const requestedThreadId = threadId;
    const requestVersion = (chatRequestVersionsRef.current.get(requestedThreadId) ?? 0) + 1;
    chatRequestVersionsRef.current.set(requestedThreadId, requestVersion);
    const isLatestRequest = () => chatRequestVersionsRef.current.get(requestedThreadId) === requestVersion;
    if (!preserveCurrent) {
      setChatLoad({
        threadId: requestedThreadId,
        loading: true,
        error: null,
      });
    }
    try {
      const next = requestedThreadId ? await api.chat(requestedThreadId) : null;
      if (viewThreadIdRef.current !== requestedThreadId || !isLatestRequest()) return;
      setChat(next);
      setChatLoad({
        threadId: requestedThreadId,
        loading: false,
        error: null,
      });
    } catch (requestError) {
      if (viewThreadIdRef.current !== requestedThreadId || !isLatestRequest() || preserveCurrent) return;
      setChat(null);
      setChatLoad({
        threadId: requestedThreadId,
        loading: false,
        error: requestError instanceof ApiError ? requestError.detail : "Could not load this chat.",
      });
    }
  }, [api, threadId]);

  useEffect(() => stop, [stop]);

  const currentChat = threadId !== null && chat?.id === threadId ? chat : null;

  useEffect(() => {
    if (isNearTranscriptEnd.current) {
      messageListRef.current?.scrollToBottom();
    }
  }, [currentChat?.messages?.length, generation.partialContent, generation.partialThoughts]);

  // New tokens while scrolled away from the bottom surface a "jump to
  // latest" affordance instead of yanking the viewport down.
  useEffect(() => {
    if (!isNearTranscriptEnd.current && (generation.partialContent || generation.partialThoughts)) {
      setShowJumpToLatest(true);
    }
  }, [generation.partialContent, generation.partialThoughts]);

  const messages = useMemo(
    () => currentChat?.messages ?? [],
    [currentChat?.messages],
  );
  const draftScope = composerDraftKey(threadId);
  const draft = drafts[draftScope] ?? readComposerDraft(threadId);
  const attachmentScope = composerAttachmentKey(threadId);
  const attachments = attachmentDrafts[attachmentScope] ?? readComposerAttachments(threadId);
  const threadOptionsKey = threadId ?? NEW_THREAD_OPTIONS_KEY;
  const threadOptions = generationOptionsByThread[threadOptionsKey] ?? null;
  const finalAssistantId = useMemo(
    () => [...messages].reverse().find((message) => message.role === "assistant")?.id ?? null,
    [messages],
  );
  const displayedThreadId = threadId ?? resolvedThreadId;
  const activeJobForCurrentThread = Boolean(generation.jobId && generation.threadId === displayedThreadId);
  const generationElsewhere = Boolean(generation.jobId && !activeJobForCurrentThread);
  const visibleGenerationError = generationError && generationError.threadId === displayedThreadId
    ? generationError.message
    : null;
  const composerPhase: ComposerPhase = !runtimeReady
    ? "unavailable"
    : generation.phase === "stopping"
      ? "stopping"
      : generation.jobId
        ? generation.contentReady ? "finishing" : "generating"
        : starting
          ? "starting"
          : "ready";

  const reconcileChat = useCallback(async (id: string): Promise<void> => {
    const requestVersion = (chatRequestVersionsRef.current.get(id) ?? 0) + 1;
    chatRequestVersionsRef.current.set(id, requestVersion);
    const isLatestRequest = () => chatRequestVersionsRef.current.get(id) === requestVersion;
    try {
      const next = await api.chat(id);
      if (!isLatestRequest()) return;
      onChatChanged(next);
      if (viewThreadIdRef.current === id) {
        setChat(next);
        setChatLoad({
          threadId: id,
          loading: false,
          error: null,
        });
      }
    } catch {
      if (!isLatestRequest()) return;
      setGenerationError({ threadId: id, message: "Generation finished, but the saved chat could not be reloaded." });
    }
  }, [api, onChatChanged]);

  const completeGeneration = useCallback(async (id: string, clearRequested = false, jobId?: string): Promise<void> => {
    await reconcileChat(id);
    if (!clearRequested) return;
    const requestKey = jobId ?? id;
    if (handledClearRequestsRef.current.has(requestKey)) return;
    handledClearRequestsRef.current.add(requestKey);
    if (!onClearMemory) {
      useUiStore.getState().notify("Cortex requested clearing permanent memories. Review Settings to confirm.", "info");
      return;
    }
    if (!window.confirm("Cortex requested clearing all permanent memories. Clear them now? This cannot be undone.")) {
      useUiStore.getState().notify("Permanent memories were not cleared.", "info");
      return;
    }
    try {
      await onClearMemory();
    } catch (error) {
      useUiStore.getState().notify(error instanceof ApiError ? error.detail : "Could not clear memories.", "error");
    }
  }, [onClearMemory, reconcileChat]);

  useEffect(() => {
    viewThreadIdRef.current = threadId;
    isNearTranscriptEnd.current = true;
    const preserveCurrent = Boolean(
      threadId !== null
      && chat?.id === threadId
      && chatLoad.threadId === threadId
      && !chatLoad.loading
      && !chatLoad.error,
    );
    const timer = window.setTimeout(() => {
      setShowJumpToLatest(false);
      setResolvedThreadId(threadId);
      void loadChat({ preserveCurrent });
      const stored = readActiveJob();
      // The global store is authoritative while the app is alive. Storage is
      // only a best-effort cold-start side channel, so a denied/quota-full
      // sessionStorage must not make a live job look finished on re-entry.
      const currentGeneration = useChatStore.getState().generation;
      const activeJob = (
        currentGeneration.jobId && currentGeneration.threadId
          ? {
              jobId: currentGeneration.jobId,
              threadId: currentGeneration.threadId,
              lastEventId: currentGeneration.lastEventId,
            }
          : null
      ) ?? stored;
      if (activeJob) {
        // Only rewind the event cursor when the store is actually cold for
        // this job. initialMountRef is per-instance, so it is true on EVERY
        // mount -- including a return from /settings, which unmounts this
        // page but leaves the module-level generation store populated.
        // Replaying from 0 onto already-accumulated text printed the answer
        // twice; keeping the stored cursor resumes where the buffer left off.
        const warmForThisJob = currentGeneration.jobId === activeJob.jobId;
        const replayFromStart = initialMountRef.current && !warmForThisJob;
        const job: PersistedJob = replayFromStart ? { ...activeJob, lastEventId: 0 } : activeJob;
        initialMountRef.current = false;
        if (replayFromStart || !warmForThisJob) {
          useChatStore.getState().beginGeneration(job.jobId, job.threadId);
        }
        void consume(job, completeGeneration, reportGenerationFailure);
      } else {
        initialMountRef.current = false;
        const current = useChatStore.getState().generation;
        if (current.jobId !== null) useChatStore.getState().endGeneration(current.jobId);
      }
    }, 0);
    return () => window.clearTimeout(timer);
    // The event consumer intentionally survives route changes. A generation
    // is global to the local backend, while a route is merely a view of it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, loadChat]);

  const startGeneration = async (
    prompt: string,
    regenerateMessageId?: string,
    suppliedAttachments: readonly ChatAttachment[] = attachments,
    requestIdOverride?: string,
    admissionOverride?: PendingAdmission,
  ): Promise<StartedGeneration | null> => {
    const input = prompt.trim() || (suppliedAttachments.length ? "Please review the attached file(s)." : "");
    if (!input || generation.jobId || startingRef.current) return null;
    if (!runtimeReady) {
      setGenerationError({
        threadId,
        message: runtimeMessage ?? "The local runtime is unavailable. Rescan local models after it is running.",
      });
      return null;
    }

    startingRef.current = true;
    setStarting(true);
    setLastPrompt(input);
    setLastAttachments([...suppliedAttachments]);
    setGenerationError(null);
    const requestId = requestIdOverride ?? createRequestId();
    const requestThreadId = admissionOverride ? admissionOverride.threadId : threadId;
    const options = admissionOverride ? admissionOverride.options : threadOptions ?? undefined;
    const baseRevision = admissionOverride
      ? admissionOverride.baseRevision
      : currentChat?.revision ?? 0;
    const pendingAdmission: PendingAdmission = admissionOverride ?? {
      requestId,
      operation: regenerateMessageId ? "regenerate" : "generate",
      threadId: requestThreadId,
      baseRevision: regenerateMessageId ? undefined : baseRevision,
      options,
      messageId: regenerateMessageId,
    };
    try {
      if (!requestIdOverride) pendingAdmissionRef.current = pendingAdmission;
      const accepted = regenerateMessageId
        ? await api.regenerate(requestThreadId ?? "", {
            request_id: requestId,
            message_id: regenerateMessageId,
            user_input: input,
            attachments: [...suppliedAttachments],
            options,
          })
        : await api.generate({
            request_id: requestId,
            thread_id: requestThreadId,
            user_input: input,
            attachments: [...suppliedAttachments],
            base_revision: baseRevision,
            options,
          });
      const jobThreadId = accepted.thread_id ?? requestThreadId;
      if (!jobThreadId) throw new Error("Cortex did not return a chat thread.");

      setResolvedThreadId(jobThreadId);
      start(accepted.job_id, jobThreadId, completeGeneration, reportGenerationFailure);
      if (pendingAdmissionRef.current?.requestId === requestId) {
        pendingAdmissionRef.current = null;
      }
      if (!regenerateMessageId) {
        setChat((current) => ({
          id: jobThreadId,
          title: current?.id === jobThreadId ? current.title : "New Chat",
          timestamp: current?.id === jobThreadId ? current.timestamp : new Date().toISOString(),
          revision: (
            current?.id === jobThreadId ? current.revision ?? 0 : 0
          ) + 1,
          messages: accepted.user_message_id
            && current?.id === jobThreadId
            && current.messages?.some((message) => message.id === accepted.user_message_id)
            ? current.messages
            : [
                ...(current?.id === jobThreadId ? current.messages ?? [] : []),
                {
                  id: accepted.user_message_id ?? undefined,
                  role: "user",
                  content: input,
                  attachments: [...suppliedAttachments],
                },
              ],
        }));
        setChatLoad({
          threadId: jobThreadId,
          loading: false,
          error: null,
        });
      }
      return { threadId: jobThreadId };
    } catch (requestError) {
      // A response with a client-side rejection is authoritative: the
      // admission did not happen and its key must not leak into a later
      // retry. Network failures and server errors remain ambiguous because
      // the backend may have admitted the job before the response was lost.
      if (
        requestError instanceof ApiError
        && requestError.status >= 400
        && requestError.status < 500
        && pendingAdmissionRef.current?.requestId === requestId
      ) {
        pendingAdmissionRef.current = null;
      }
      setGenerationError({
        threadId,
        message: requestError instanceof ApiError ? requestError.detail : "The response could not be started. Your message is still here.",
      });
      return null;
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  const submitDraft = async (): Promise<boolean> => {
    const submittedDraft = draft;
    const submittedAttachments = attachments;
    const submittedScope = draftScope;
    const submittedAttachmentScope = attachmentScope;
    const submittedThreadId = threadId;
    const started = await startGeneration(submittedDraft, undefined, submittedAttachments);
    if (!started) return false;

    const destinationThreadId = submittedThreadId ?? started.threadId;
    const destinationDraftScope = composerDraftKey(destinationThreadId);
    const destinationAttachmentScope = composerAttachmentKey(destinationThreadId);
    if (!submittedThreadId) {
      // Overrides staged before the first message live under the "new chat"
      // placeholder key. Migrate them to the real thread id now that one
      // exists, so they keep applying to this conversation instead of
      // reverting after one message and leaking into the next new chat.
      const draftOptions = useChatStore.getState().generationOptionsByThread[NEW_THREAD_OPTIONS_KEY];
      if (draftOptions) {
        setThreadOptions(destinationThreadId, draftOptions);
        setThreadOptions(NEW_THREAD_OPTIONS_KEY, null);
      }
    }
    if (submittedAttachmentScope !== destinationAttachmentScope) {
      // Retarget only batches that were already staging into this submitted
      // draft. Each batch owns its mutable target, so a later /chat/new never
      // inherits a stale redirect to this accepted thread.
      for (const target of attachmentDraftTargetsRef.current) {
        if (target.scope === submittedAttachmentScope) {
          target.scope = destinationAttachmentScope;
          target.threadId = destinationThreadId;
        }
      }
    }
    const currentDraft = draftsRef.current[submittedScope] ?? readComposerDraft(submittedThreadId);
    const retainedDraft = currentDraft === submittedDraft ? "" : currentDraft;
    if (submittedScope === destinationDraftScope) {
      const nextDrafts = { ...draftsRef.current, [submittedScope]: retainedDraft };
      draftsRef.current = nextDrafts;
      setDrafts(nextDrafts);
      writeComposerDraft(submittedThreadId, retainedDraft);
    } else {
      const nextDrafts = {
        ...draftsRef.current,
        [submittedScope]: "",
        [destinationDraftScope]: retainedDraft,
      };
      draftsRef.current = nextDrafts;
      setDrafts(nextDrafts);
      writeComposerDraft(submittedThreadId, "");
      writeComposerDraft(destinationThreadId, retainedDraft);
    }
    if (submittedAttachments.length || submittedAttachmentScope !== destinationAttachmentScope) {
      const submittedAttachmentIds = new Set(submittedAttachments.map((attachment) => attachment.attachment_id));
      const currentAttachments = attachmentDraftsRef.current[submittedAttachmentScope]
        ?? readComposerAttachments(submittedThreadId);
      const retainedAttachments = currentAttachments.filter(
        (attachment) => !submittedAttachmentIds.has(attachment.attachment_id),
      );
      const nextAttachments = submittedAttachmentScope === destinationAttachmentScope
        ? { ...attachmentDraftsRef.current, [submittedAttachmentScope]: retainedAttachments }
        : {
            ...attachmentDraftsRef.current,
            [submittedAttachmentScope]: [],
            [destinationAttachmentScope]: retainedAttachments,
          };
      attachmentDraftsRef.current = nextAttachments;
      setAttachmentDrafts(nextAttachments);
      if (submittedAttachmentScope !== destinationAttachmentScope) {
        writeComposerAttachments(submittedThreadId, []);
      }
      writeComposerAttachments(destinationThreadId, retainedAttachments);
    }
    if (!submittedThreadId) onThreadCreated(started.threadId);
    return true;
  };

  const cancel = async (): Promise<void> => {
    const active = useChatStore.getState().generation;
    if (!active.jobId || stoppingRef.current) return;
    const jobId = active.jobId;
    const jobThreadId = active.threadId;
    stoppingRef.current = true;
    useChatStore.getState().markStopping(jobId);
    useChatStore.getState().setStatusText(jobId, "Stopping response...");
    try {
      const snapshot = await api.cancelGeneration(jobId);
      if (snapshot.status !== "cancelling" && snapshot.status !== "cancelled") {
        // Persistence has already crossed the backend's commit barrier, so a
        // late stop is intentionally inert. Reflect that response instead of
        // leaving the composer disabled in a false "Stopping" state while the
        // durable answer finishes its optional bookkeeping.
        useChatStore.getState().markContentReady(jobId);
        useChatStore.getState().revertStopping(jobId);
        useChatStore.getState().setStatusText(jobId, "Finishing response...");
      }
    } catch (requestError) {
      useChatStore.getState().revertStopping(jobId);
      setGenerationError({
        threadId: jobThreadId,
        message: requestError instanceof ApiError ? requestError.detail : "Could not stop the response.",
      });
    } finally {
      stoppingRef.current = false;
    }
  };

  const retryLastPrompt = async (): Promise<boolean> => {
    if (!lastPrompt) return false;
    const pendingAdmission = pendingAdmissionRef.current;
    const started = await startGeneration(
      lastPrompt,
      pendingAdmission?.operation === "regenerate" ? pendingAdmission.messageId : undefined,
      lastAttachments,
      pendingAdmission?.requestId,
      pendingAdmission ?? undefined,
    );
    if (started && !threadId) onThreadCreated(started.threadId);
    return Boolean(started);
  };

  const fork = async (message: ChatMessage) => {
    if (!threadId || !message.id || forkingMessage || generation.jobId || starting) return;
    setForkingMessage(message.id);
    try {
      const forked = await api.forkChat(threadId, message.id);
      onForked(forked);
    } catch (requestError) {
      setGenerationError({
        threadId,
        message: requestError instanceof ApiError ? requestError.detail : "Could not fork this chat.",
      });
    } finally {
      setForkingMessage(null);
    }
  };

  const updateDraft = (nextDraft: string) => {
    const nextDrafts = { ...draftsRef.current, [draftScope]: nextDraft };
    draftsRef.current = nextDrafts;
    setDrafts(nextDrafts);
    writeComposerDraft(threadId, nextDraft);
  };

  const addAttachments = async (files: File[]): Promise<void> => {
    if (attachmentsBusy || !files.length) return;
    const target: AttachmentDraftTarget = { scope: attachmentScope, threadId };
    attachmentDraftTargetsRef.current.add(target);
    setAttachmentsBusy(true);
    setAttachmentError(null);
    try {
      const remaining = Math.max(0, MAX_CHAT_ATTACHMENTS - attachments.length);
      if (!remaining) throw new Error("A message can include at most eight attachments.");
      let totalBytes = attachments.reduce((total, attachment) => total + attachment.size, 0);
      const staged: ChatAttachment[] = [];
      for (const file of files.slice(0, remaining)) {
        if (!file.size || file.size > MAX_CHAT_ATTACHMENT_BYTES) {
          throw new Error(`${file.name} is empty or larger than 10 MB.`);
        }
        if (totalBytes + file.size > MAX_CHAT_ATTACHMENT_TOTAL_BYTES) {
          throw new Error("The combined attachment size is too large for one message.");
        }
        const contentBase64 = await fileToBase64(file);
        const attachment = await api.stageChatAttachment({
          request_id: createRequestId(),
          filename: file.name,
          content_base64: contentBase64,
        });
        staged.push(attachment);
        totalBytes += attachment.size;
      }
      // The generation request and attachment staging can finish in either
      // order. Merge into the latest scoped draft instead of the render-time
      // `attachments` snapshot, which may contain files that were submitted
      // and cleared while these new files were still uploading.
      const currentAttachments = attachmentDraftsRef.current[target.scope]
        ?? readComposerAttachments(target.threadId);
      const currentAttachmentIds = new Set(currentAttachments.map((attachment) => attachment.attachment_id));
      const next = [
        ...currentAttachments,
        ...staged.filter((attachment) => !currentAttachmentIds.has(attachment.attachment_id)),
      ];
      const nextAttachments = { ...attachmentDraftsRef.current, [target.scope]: next };
      attachmentDraftsRef.current = nextAttachments;
      setAttachmentDrafts(nextAttachments);
      writeComposerAttachments(target.threadId, next);
    } catch (error) {
      setAttachmentError(error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "The attachment could not be uploaded.");
    } finally {
      attachmentDraftTargetsRef.current.delete(target);
      setAttachmentsBusy(false);
    }
  };

  const removeAttachment = (attachmentId: string) => {
    const next = attachments.filter((attachment) => attachment.attachment_id !== attachmentId);
    const nextAttachments = { ...attachmentDraftsRef.current, [attachmentScope]: next };
    attachmentDraftsRef.current = nextAttachments;
    setAttachmentDrafts(nextAttachments);
    writeComposerAttachments(threadId, next);
    setAttachmentError(null);
  };

  const imageInputBlocked = attachments.some((attachment) => attachment.kind === "image")
    && selectedModelSupportsVision === false
    ? `Selected model "${selectedModel ?? "this model"}" cannot accept images. Choose a vision model or remove the image.`
    : null;

  const handleNearEndChange = (isNearEnd: boolean) => {
    isNearTranscriptEnd.current = isNearEnd;
    if (isNearEnd) setShowJumpToLatest(false);
  };

  const jumpToLatest = () => {
    messageListRef.current?.scrollToBottom();
    isNearTranscriptEnd.current = true;
    setShowJumpToLatest(false);
  };

  if (chatLoad.threadId !== threadId || chatLoad.loading) return <div className="chat-empty-state" aria-live="polite"><span className="loading-spinner" />Loading conversation...</div>;
  if (chatLoad.error) return <div className="chat-empty-state"><h2>Conversation unavailable</h2><p>{chatLoad.error}</p><button className="button button-primary" onClick={() => void loadChat()}>Retry</button></div>;

  return (
    <section className="chat-page" aria-labelledby="chat-title">
      <h2 id="chat-title" className="sr-only">{displayChatTitle(currentChat?.title, "New Chat")}</h2>
      <MessageList
        ref={messageListRef}
        messages={messages}
        isStreaming={activeJobForCurrentThread}
        finalAssistantId={finalAssistantId}
        busy={Boolean(generation.jobId) || starting}
        forkingMessageId={forkingMessage}
        onRegenerate={(message, index) => {
          const userTurn = messages[index - 1];
          void startGeneration(
            userTurn?.role === "user" ? userTurn.content : "",
            message.id ?? undefined,
            userTurn?.role === "user" ? userTurn.attachments ?? [] : [],
          );
        }}
        onFork={(message) => void fork(message)}
        onNearEndChange={handleNearEndChange}
        trailingContent={
          <>
            {activeJobForCurrentThread && !generation.partialContent && !generation.partialThoughts && <GenerationStatus status={generation.statusText} />}
            {activeJobForCurrentThread && (generation.partialContent || generation.partialThoughts) && (
              /* Same markup and the same unframed treatment as a persisted
                 assistant message, so when this is replaced by the real one
                 nothing about the message changes shape or position. */
              <article className="message-card message-assistant message-pending" aria-label={generation.contentReady ? "Cortex response ready, saving..." : "Cortex response in progress"}>
                <div className="message-bubble">
                  {generation.partialContent && <div className="markdown-body"><SafeMarkdown content={generation.partialContent} finalized={generation.contentReady} />{!generation.contentReady && <span className="streaming-caret" aria-hidden="true" />}</div>}
                  {!generation.partialContent && !generation.contentReady && <span className="streaming-caret" aria-hidden="true" />}
                </div>
                {/* Collapses in step with the "Live" badge, matching the persisted card's default state so the swap is invisible. */}
                {generation.partialThoughts && <details className="reasoning" open={!generation.contentReady}><summary><span>Reasoning</span>{!generation.contentReady && <span className="disclosure-hint">Live</span>}</summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={generation.partialThoughts} finalized={generation.contentReady} /></div></div></details>}
              </article>
            )}
          </>
        }
      />
      <div className="input-container composer-dock">
        {showJumpToLatest && <button className="jump-to-latest" type="button" onClick={jumpToLatest}>Jump to latest</button>}
        <MessageComposer
          value={draft}
          phase={composerPhase}
          selectedModel={selectedModel}
          attachments={attachments}
          attachmentsBusy={attachmentsBusy}
          attachmentError={attachmentError}
          imageInputBlocked={imageInputBlocked}
          onAddAttachments={addAttachments}
          onRemoveAttachment={removeAttachment}
          localModels={localModels}
          runtimeMessage={runtimeMessage}
          generationElsewhere={generationElsewhere}
          modelBusy={modelBusy}
          error={visibleGenerationError}
          onValueChange={updateDraft}
          onSubmit={submitDraft}
          onStop={cancel}
          onSelectModel={onSelectModel}
          onRescanModels={onRescanModels}
          onRetry={lastPrompt ? retryLastPrompt : undefined}
          onDismissError={() => setGenerationError(null)}
          generationOptions={threadOptions}
          generationDefaults={generationDefaults}
          onGenerationOptionsChange={(next) => setThreadOptions(threadOptionsKey, next)}
        />
      </div>
    </section>
  );
}

function GenerationStatus({ status }: { status: string }) {
  return (
    <article className="message-card message-assistant message-pending" aria-label="Cortex response in progress">
      <div className="message-bubble">
        <div className="generation-status" role="status">
          {humanizeGenerationStatus(status)}
          <span className="generation-status-dots" aria-hidden="true"><i /><i /><i /></span>
        </div>
      </div>
    </article>
  );
}

function createRequestId(): string {
  return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}
