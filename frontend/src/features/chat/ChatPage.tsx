import { Paperclip } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ChatAttachment, ChatMessage, ChatResponse, GenerationOptionsOverride } from "../../../../contracts/cortex-api";
import { ApiError, CortexApi, describeApiError, isDefinitiveRejection } from "../../api/client";
import { displayChatTitle } from "../../lib/chatTitle";
import { describeUnsupportedFiles, splitSupportedFiles } from "../../lib/attachments";
import { composerAttachmentKey, composerDraftKey, readComposerAttachments, readComposerDraft, writeComposerAttachments, writeComposerDraft } from "../../lib/composerDraft";
import { useShallow } from "zustand/react/shallow";
import { useFileDropZone } from "../../hooks/useFileDropZone";
import { trackGeneration } from "../../hooks/useGenerationStream";
import { NEW_THREAD_OPTIONS_KEY, useChatStore } from "../../stores/useChatStore";
import { useSettingsStore } from "../../stores/useSettingsStore";
import { useUiStore } from "../../stores/useUiStore";
import { Dialog, DialogContent } from "../../shared/ui/Dialog";
import { MessageComposer, type ComposerPhase } from "./MessageComposer";
import { MessageList, type MessageListHandle } from "./MessageList";
import { PendingAssistantMessage } from "./PendingAssistantMessage";

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
  onForked: (chat: ChatResponse) => void;
  onClearMemory?: () => Promise<void>;
};

type ScopedError = {
  message: string;
  threadId: string | null;
  // Whether resending the last prompt is the remedy for this error. The
  // banner offers one Retry button for every failure it can show, and most
  // of them are not generation failures at all -- a failed fork, a failed
  // stop, a reload that failed after the answer was already saved. Retrying
  // the prompt there resubmits a turn the thread has already answered.
  retryable: boolean;
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
  onForked,
  onClearMemory,
}: Props) {
  // Only the fields this page acts on. The streamed text is deliberately not
  // among them: the store rebuilds `generation` on every flushed frame, and
  // reading it here re-rendered the whole page -- every persisted message and
  // the composer -- for text that only the pending bubble draws (see
  // PendingAssistantMessage). `useShallow` keeps this a no-op re-render unless
  // one of these four values actually changes.
  const generation = useChatStore(useShallow((state) => ({
    jobId: state.generation.jobId,
    threadId: state.generation.threadId,
    phase: state.generation.phase,
    contentReady: state.generation.contentReady,
  })));
  const generationOptionsByThread = useChatStore((state) => state.generationOptionsByThread);
  const setThreadOptions = useChatStore((state) => state.setThreadOptions);
  const generationDefaults = useSettingsStore((state) => state.settings?.generation) ?? DEFAULT_GENERATION_SETTINGS;
  // How the last generation ended, published by GenerationStreamHost -- which
  // keeps consuming the stream while this page is not mounted. Handled by the
  // two effects further down.
  const lastCompletion = useChatStore((state) => state.lastCompletion);
  const lastFailure = useChatStore((state) => state.lastFailure);
  // Outcomes already on the store when this page mounted happened while it was
  // away. Their chat is not applied (the load below fetches the current one).
  const [mountCompletionId] = useState(() => useChatStore.getState().lastCompletion?.id ?? 0);
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
  // Thumbnails are object URLs of the files as they were picked, by attachment
  // id. They exist only for this page's lifetime: a draft restored after a
  // reload has none and shows the file icon instead.
  const [attachmentPreviews, setAttachmentPreviews] = useState<Record<string, string>>({});
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [memoryClearPromptOpen, setMemoryClearPromptOpen] = useState(false);
  const startingRef = useRef(false);
  const stoppingRef = useRef(false);
  const messageListRef = useRef<MessageListHandle>(null);
  const isNearTranscriptEnd = useRef(true);
  const viewThreadIdRef = useRef<string | null>(threadId);
  // Read inside async callbacks, which would otherwise close over a stale
  // `chat` from the render that created them.
  const chatRef = useRef<ChatResponse | null>(null);
  const chatRequestVersionsRef = useRef(new Map<string | null, number>());
  const draftsRef = useRef(drafts);
  const attachmentDraftsRef = useRef(attachmentDrafts);
  const attachmentDraftTargetsRef = useRef(new Set<AttachmentDraftTarget>());
  // Batches are staged one after another, so a second drop or paste while the
  // first is still uploading is queued rather than lost.
  const attachmentQueueRef = useRef<Promise<void>>(Promise.resolve());
  const pendingAttachmentBatchesRef = useRef(0);
  const attachmentPreviewsRef = useRef(attachmentPreviews);
  // A POST can be admitted before its response reaches the browser. Keep its
  // idempotency key across that ambiguous failure so Retry can replay the
  // admission instead of creating a second job (or a second new-chat thread).
  // A deliberate submit does not pass this key and therefore starts a new
  // user turn with a fresh request id.
  const pendingAdmissionRef = useRef<PendingAdmission | null>(null);
  const handledClearRequestsRef = useRef(new Set<string>());
  const handledCompletionsRef = useRef(new Set<number>());

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
        error: describeApiError(requestError, "Could not load this chat."),
      });
    }
  }, [api, threadId]);

  useEffect(() => {
    chatRef.current = chat;
  }, [chat]);

  const currentChat = threadId !== null && chat?.id === threadId ? chat : null;

  useEffect(() => {
    if (isNearTranscriptEnd.current) {
      messageListRef.current?.scrollToBottom();
    }
  }, [currentChat?.messages?.length]);

  // The pending bubble reports each change to the visible reply. A reader at
  // the bottom is followed down; new text while scrolled away surfaces a "jump
  // to latest" affordance instead of yanking the viewport. Stable, because it
  // is a prop of a component that renders once per streamed frame.
  const followStreamedOutput = useCallback(() => {
    if (isNearTranscriptEnd.current) {
      messageListRef.current?.scrollToBottom();
    } else {
      setShowJumpToLatest(true);
    }
  }, []);

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
  const visibleError = generationError && generationError.threadId === displayedThreadId
    ? generationError
    : null;
  const visibleGenerationError = visibleError?.message ?? null;
  const composerPhase: ComposerPhase = !runtimeReady
    ? "unavailable"
    : generation.phase === "stopping"
      ? "stopping"
      : generation.jobId
        ? generation.contentReady ? "finishing" : "generating"
        : starting
          ? "starting"
          : "ready";

  // A generation failed: show it for the thread it belongs to. A failure that
  // came in while this page was away is waiting on the store and shows here on
  // return. Resending the prompt is the remedy, so it is retryable.
  useEffect(() => {
    if (!lastFailure) return;
    useChatStore.getState().acknowledgeFailure(lastFailure.id);
    // The store is the external system here: an outcome published there is
    // taken into local state exactly once, and acknowledged so it is not taken
    // again by the next mount. It happens once per generation, not per render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setGenerationError({ threadId: lastFailure.threadId, message: lastFailure.message, retryable: true });
  }, [lastFailure]);

  // A generation ended and the stream host has already reloaded its chat.
  //
  // A layout effect on purpose: the host drops the pending bubble as soon as
  // this outcome is published, so the saved message has to be in `chat` before
  // then -- from a passive effect it would land a frame later and the answer
  // would blink out in between.
  useLayoutEffect(() => {
    if (!lastCompletion || handledCompletionsRef.current.has(lastCompletion.id)) return;
    const completion = lastCompletion;
    handledCompletionsRef.current.add(completion.id);
    useChatStore.getState().acknowledgeCompletion(completion.id);
    const id = completion.threadId;
    // One that predates this mount ended while the page was away: the load
    // this mount runs already fetches the current chat, so only its side
    // effects (the memory prompt below) still apply.
    if (completion.id > mountCompletionId) {
      // A load still in flight for this thread began before the answer was
      // saved; it must not replace the chat that includes it.
      chatRequestVersionsRef.current.set(id, (chatRequestVersionsRef.current.get(id) ?? 0) + 1);
      if (completion.chat) {
        if (viewThreadIdRef.current === id) {
          setChat(completion.chat);
          setChatLoad({ threadId: id, loading: false, error: null });
        }
      } else {
        // The generation succeeded; only the reload failed. Resending would
        // ask for a second answer to a question already answered.
        // eslint-disable-next-line react-hooks/set-state-in-effect -- same as the failure effect above: an outcome from the store, handled once
        setGenerationError({ threadId: id, message: "Generation finished, but the saved chat could not be reloaded.", retryable: false });
        if (viewThreadIdRef.current === id) {
          // Invalidating the version above already made any route load for
          // this thread return early as stale. If we do not settle the load
          // state here too, nothing ever will: the page stays on "Loading
          // conversation..." forever, and that branch renders before the
          // error branch, so there is not even a Retry button.
          setChatLoad((current) => {
            if (current.threadId !== id) return current;
            // The transcript we already have is stale but readable, so keep
            // showing it with the banner. With nothing to show, surface the
            // failure so Retry is reachable.
            if (!current.loading && !current.error) return current;
            return chatRef.current
              ? { threadId: id, loading: false, error: null }
              : {
                  threadId: id,
                  loading: false,
                  error: "Could not reload this chat after the generation finished.",
                };
          });
        }
      }
    }
    if (!completion.clearRequested) return;
    const requestKey = completion.jobId ?? id;
    if (handledClearRequestsRef.current.has(requestKey)) return;
    handledClearRequestsRef.current.add(requestKey);
    if (!onClearMemory) {
      useUiStore.getState().notify("Cortex requested clearing permanent memories. Review Settings to confirm.", "info");
      return;
    }
    // Ask via the app's own dialog rather than window.confirm(): that native
    // call is synchronous and blocks the whole JS thread -- including the
    // pending message bubble's teardown -- until the OS dialog is dismissed.
    setMemoryClearPromptOpen(true);
  }, [lastCompletion, mountCompletionId, onClearMemory]);

  // A memory the model suggested is stored only here, when the user presses
  // Save. It goes through the same memories API as the Settings panel, so the
  // store applies its own limits and de-duplication.
  const saveProposedMemory = useCallback(async (memo: string): Promise<boolean> => {
    try {
      await api.addMemory(memo);
      useUiStore.getState().notify("Memory saved.", "success");
      return true;
    } catch (error) {
      useUiStore.getState().notify(describeApiError(error, "Could not save memory."), "error");
      return false;
    }
  }, [api]);

  const cancelMemoryClear = useCallback(() => {
    setMemoryClearPromptOpen(false);
    useUiStore.getState().notify("Permanent memories were not cleared.", "info");
  }, []);

  const confirmMemoryClear = useCallback(async () => {
    try {
      await onClearMemory?.();
    } catch (error) {
      useUiStore.getState().notify(describeApiError(error, "Could not clear memories."), "error");
    } finally {
      setMemoryClearPromptOpen(false);
    }
  }, [onClearMemory]);

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
    }, 0);
    return () => window.clearTimeout(timer);
    // Re-run for a different thread (or a new loadChat, which follows the
    // thread), not for every change of the chat and load state read above. The
    // running generation is not touched here: GenerationStreamHost owns its
    // stream, and this page only reads the text from the store.
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
        retryable: true,
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
      trackGeneration(accepted.job_id, jobThreadId);
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
      if (isDefinitiveRejection(requestError) && pendingAdmissionRef.current?.requestId === requestId) {
        pendingAdmissionRef.current = null;
      }
      setGenerationError({
        threadId,
        message: describeApiError(requestError, "The response could not be started. Your message is still here."),
        // A validation error says the request itself is wrong; sending the
        // same prompt again can only fail the same way, so the banner offers
        // no Retry and leaves the fix to the draft.
        retryable: !(requestError instanceof ApiError && requestError.kind === "validation"),
      });
      return null;
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  // Everything the composer has to let go of once a message is genuinely on
  // its way: the draft text, its attachments, and -- for a first message --
  // the migration of both from the "new chat" placeholder scope to the real
  // thread. Retry sends a message too, and used to skip all of it, leaving
  // the text it had just sent sitting in the box ready to be sent twice.
  const settleComposerAfterSend = (
    started: StartedGeneration,
    submittedDraft: string,
    submittedAttachments: readonly ChatAttachment[],
    submittedScope: string,
    submittedAttachmentScope: string,
    submittedThreadId: string | null,
  ) => {
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
  };

  const submitDraft = async (): Promise<boolean> => {
    const submittedDraft = draft;
    const submittedAttachments = attachments;
    const submittedScope = draftScope;
    const submittedAttachmentScope = attachmentScope;
    const submittedThreadId = threadId;
    const started = await startGeneration(submittedDraft, undefined, submittedAttachments);
    if (!started) return false;

    settleComposerAfterSend(
      started,
      submittedDraft,
      submittedAttachments,
      submittedScope,
      submittedAttachmentScope,
      submittedThreadId,
    );
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
      // Stop failed, so the response is still running. Sending the prompt
      // again would start a second one alongside it.
      setGenerationError({
        threadId: jobThreadId,
        message: describeApiError(requestError, "Could not stop the response."),
        retryable: false,
      });
    } finally {
      stoppingRef.current = false;
    }
  };

  const retryLastPrompt = async (): Promise<boolean> => {
    if (!lastPrompt) return false;
    // A failed send deliberately leaves the text in the composer ("Your
    // message is still here"), so a retry that succeeds has to clear it the
    // way a submit does -- otherwise the message is both in the transcript
    // and back in the box, one Enter away from being sent twice.
    //
    // `lastPrompt` is the trimmed input, so compare on that: only the draft
    // that is still the message being retried is cleared, and a draft the
    // user has since started rewriting is left alone.
    const submittedDraft = draft.trim() === lastPrompt ? draft : "";
    const submittedScope = draftScope;
    const submittedAttachmentScope = attachmentScope;
    const submittedThreadId = threadId;
    const settle = (started: StartedGeneration) => settleComposerAfterSend(
      started,
      submittedDraft,
      lastAttachments,
      submittedScope,
      submittedAttachmentScope,
      submittedThreadId,
    );
    // A stream-level failure after the user's message was already durably
    // admitted leaves that message as the thread's last one with no reply.
    // Retrying must regenerate a reply for it, not resubmit the same text
    // as a brand new message -- which used to duplicate the user's turn.
    // The backend's dangling-user-turn regenerate support exists exactly
    // for this case.
    const messages = chat?.messages;
    const lastMessage = messages && messages.length > 0 ? messages[messages.length - 1] : undefined;
    const danglingUserMessageId = lastMessage?.role === "user" ? lastMessage.id ?? undefined : undefined;
    if (danglingUserMessageId) {
      const started = await startGeneration(lastPrompt, danglingUserMessageId, lastAttachments);
      if (started) settle(started);
      if (started && !threadId) onThreadCreated(started.threadId);
      return Boolean(started);
    }
    // Otherwise the failure was ambiguous -- the admission POST itself may
    // or may not have reached the backend -- so replay the same request id
    // instead, which the backend treats idempotently if it already landed.
    const pendingAdmission = pendingAdmissionRef.current;
    const started = await startGeneration(
      lastPrompt,
      pendingAdmission?.operation === "regenerate" ? pendingAdmission.messageId : undefined,
      lastAttachments,
      pendingAdmission?.requestId,
      pendingAdmission ?? undefined,
    );
    if (started) settle(started);
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
      // Forking is not a generation. The thread is answered and unchanged.
      setGenerationError({
        threadId,
        message: describeApiError(requestError, "Could not fork this chat."),
        retryable: false,
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

  const stageAttachmentBatch = async (target: AttachmentDraftTarget, files: File[]): Promise<void> => {
    // Each staged file is already uploaded and already holding backend
    // retention, so it belongs in the composer whether or not a later file in
    // the same batch fails. Committing only after the whole loop meant one bad
    // file discarded every good one before it -- leaving those artifacts
    // orphaned on the backend and making the user re-add the rest by hand.
    const staged: ChatAttachment[] = [];
    const previews: Record<string, string> = {};
    const commitStaged = () => {
      if (!staged.length) return;
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
      if (Object.keys(previews).length) setAttachmentPreviews((current) => ({ ...current, ...previews }));
    };
    try {
      // Measured when this batch starts, not when it was queued: an earlier
      // batch may have added files, and a message may have been sent since.
      const current = attachmentDraftsRef.current[target.scope] ?? readComposerAttachments(target.threadId);
      const remaining = Math.max(0, MAX_CHAT_ATTACHMENTS - current.length);
      if (!remaining) throw new Error(`A message can include at most ${MAX_CHAT_ATTACHMENTS} attachments.`);
      let totalBytes = current.reduce((total, attachment) => total + attachment.size, 0);
      const accepted = files.slice(0, remaining);
      for (const file of accepted) {
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
        if (attachment.kind === "image" && typeof URL.createObjectURL === "function") {
          previews[attachment.attachment_id] = URL.createObjectURL(file);
        }
        totalBytes += attachment.size;
      }
      commitStaged();
      if (accepted.length < files.length) {
        // Dropping the overflow silently looked exactly like attaching it.
        setAttachmentError(
          `Only ${accepted.length} of ${files.length} files were attached; a message can include at most ${MAX_CHAT_ATTACHMENTS}.`,
        );
      }
    } catch (error) {
      commitStaged();
      const detail = describeApiError(error, error instanceof Error ? error.message : "The attachment could not be uploaded.");
      setAttachmentError(staged.length ? `${detail} Files attached before it were kept.` : detail);
    } finally {
      attachmentDraftTargetsRef.current.delete(target);
    }
  };

  const addAttachments = (files: File[]): Promise<void> => {
    if (!files.length) return Promise.resolve();
    // Files the backend would refuse are turned away before they are uploaded.
    // The rest still go through if some were refused.
    const { supported, rejected } = splitSupportedFiles(files);
    setAttachmentError(rejected.length ? describeUnsupportedFiles(rejected) : null);
    if (!supported.length) return Promise.resolve();
    // The destination is fixed now: a batch queued in one chat stays in that
    // chat even if the user has switched threads by the time it runs.
    const target: AttachmentDraftTarget = { scope: attachmentScope, threadId };
    attachmentDraftTargetsRef.current.add(target);
    pendingAttachmentBatchesRef.current += 1;
    setAttachmentsBusy(true);
    const batch = attachmentQueueRef.current
      .then(() => stageAttachmentBatch(target, supported))
      .finally(() => {
        pendingAttachmentBatchesRef.current -= 1;
        if (pendingAttachmentBatchesRef.current === 0) setAttachmentsBusy(false);
      });
    // A failure in one batch must not stop the ones queued behind it.
    attachmentQueueRef.current = batch.catch(() => undefined);
    return batch;
  };

  useEffect(() => {
    attachmentPreviewsRef.current = attachmentPreviews;
  }, [attachmentPreviews]);

  // Release a thumbnail once its attachment is no longer staged anywhere, and
  // all of them when the page goes away. The state keeps the (now dead) string,
  // but the composer only ever asks for the ids it is showing, and revoking
  // twice is harmless.
  useEffect(() => {
    const staged = new Set(Object.values(attachmentDrafts).flat().map((attachment) => attachment.attachment_id));
    for (const [id, url] of Object.entries(attachmentPreviews)) {
      if (!staged.has(id)) URL.revokeObjectURL(url);
    }
  }, [attachmentDrafts, attachmentPreviews]);
  useEffect(() => () => {
    for (const url of Object.values(attachmentPreviewsRef.current)) URL.revokeObjectURL(url);
  }, []);

  const removeAttachment = (attachmentId: string) => {
    const next = attachments.filter((attachment) => attachment.attachment_id !== attachmentId);
    const nextAttachments = { ...attachmentDraftsRef.current, [attachmentScope]: next };
    attachmentDraftsRef.current = nextAttachments;
    setAttachmentDrafts(nextAttachments);
    writeComposerAttachments(threadId, next);
    setAttachmentError(null);
  };

  // Dropping a file anywhere on the page attaches it; the composer, when it is
  // the target, handles the drop first.
  const pageDrop = useFileDropZone({ onFiles: (files) => void addAttachments(files) });

  const imageInputBlocked = attachments.some((attachment) => attachment.kind === "image")
    && selectedModelSupportsVision === false
    ? `Selected model "${selectedModel ?? "this model"}" cannot accept images. Choose a vision model or remove the image.`
    : null;

  // These two go to every transcript card, so each must keep one identity
  // across renders or the cards' memo is defeated. `startGeneration` and `fork`
  // close over live state and change every render, so they are read through a
  // ref that is refreshed after each one.
  const latestHandlers = useRef({ messages, startGeneration, fork });
  useEffect(() => {
    latestHandlers.current = { messages, startGeneration, fork };
  });
  const regenerateFrom = useCallback((message: ChatMessage, index: number) => {
    const { messages: current, startGeneration: start } = latestHandlers.current;
    const userTurn = current[index - 1];
    void start(
      userTurn?.role === "user" ? userTurn.content : "",
      message.id ?? undefined,
      userTurn?.role === "user" ? userTurn.attachments ?? [] : [],
    );
  }, []);
  const forkFrom = useCallback((message: ChatMessage) => {
    void latestHandlers.current.fork(message);
  }, []);

  const handleNearEndChange = (isNearEnd: boolean) => {
    isNearTranscriptEnd.current = isNearEnd;
    if (isNearEnd) setShowJumpToLatest(false);
  };

  const jumpToLatest = () => {
    // The one scroll the reader asked for, so the one that may animate.
    messageListRef.current?.scrollToBottom("smooth");
    isNearTranscriptEnd.current = true;
    setShowJumpToLatest(false);
  };

  if (chatLoad.threadId !== threadId || chatLoad.loading) return <div className="chat-empty-state" aria-live="polite"><span className="loading-spinner" />Loading conversation...</div>;
  if (chatLoad.error) return <div className="chat-empty-state"><h2>Conversation unavailable</h2><p>{chatLoad.error}</p><button className="button button-primary" onClick={() => void loadChat()}>Retry</button></div>;

  return (
    <section className="chat-page" aria-labelledby="chat-title" {...pageDrop.handlers}>
      {pageDrop.active && (
        <div className="drop-overlay chat-drop-overlay" aria-hidden="true">
          <Paperclip size={20} /> Drop to attach
        </div>
      )}
      <h2 id="chat-title" className="sr-only">{displayChatTitle(currentChat?.title, "New Chat")}</h2>
      <MessageList
        ref={messageListRef}
        messages={messages}
        isStreaming={activeJobForCurrentThread}
        finalAssistantId={finalAssistantId}
        busy={Boolean(generation.jobId) || starting}
        forkingMessageId={forkingMessage}
        onRegenerate={regenerateFrom}
        onFork={forkFrom}
        onSaveMemory={saveProposedMemory}
        onNearEndChange={handleNearEndChange}
        trailingContent={activeJobForCurrentThread ? <PendingAssistantMessage key={generation.jobId} onOutputChange={followStreamedOutput} /> : null}
      />
      <div className="input-container composer-dock">
        {showJumpToLatest && <button className="jump-to-latest" type="button" onClick={jumpToLatest}>Jump to latest</button>}
        <MessageComposer
          value={draft}
          phase={composerPhase}
          selectedModel={selectedModel}
          attachments={attachments}
          attachmentsBusy={attachmentsBusy}
          attachmentPreviews={attachmentPreviews}
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
          onRetry={lastPrompt && visibleError?.retryable ? retryLastPrompt : undefined}
          onDismissError={() => setGenerationError(null)}
          generationOptions={threadOptions}
          generationDefaults={generationDefaults}
          onGenerationOptionsChange={(next) => setThreadOptions(threadOptionsKey, next)}
        />
      </div>
      {memoryClearPromptOpen && (
        <MemoryClearConfirmDialog onCancel={cancelMemoryClear} onConfirm={confirmMemoryClear} />
      )}
    </section>
  );
}

/**
 * Confirms Cortex's in-band request to clear permanent memories. A plain
 * yes/no dialog (mirroring DeleteGroupDialog in ChatLibrary) rather than
 * window.confirm(), which blocks the whole JS thread -- including the
 * pending message bubble's teardown -- until the native dialog is dismissed.
 */
function MemoryClearConfirmDialog({
  onCancel,
  onConfirm,
}: {
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog.Root open onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogContent>
        <Dialog.Title>Clear permanent memories?</Dialog.Title>
        <p className="delete-dialog-description">Cortex requested clearing all permanent memories. Clear them now? This cannot be undone.</p>
        <div className="dialog-actions">
          <button type="button" className="button button-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            type="button"
            className="button button-danger"
            disabled={busy}
            onClick={async () => {
              if (busy) return;
              setBusy(true);
              try { await onConfirm(); } finally { setBusy(false); }
            }}
          >
            {busy ? "Clearing…" : "Clear memories"}
          </button>
        </div>
      </DialogContent>
    </Dialog.Root>
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
