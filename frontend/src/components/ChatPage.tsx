import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Copy, FileText, GitBranch, Image as ImageIcon, RefreshCw } from "lucide-react";
import type { ChatAttachment, ChatMessage, ChatResponse } from "../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../api/client";
import { displayChatTitle } from "../lib/chatTitle";
import { composerAttachmentKey, composerDraftKey, readComposerAttachments, readComposerDraft, writeComposerAttachments, writeComposerDraft } from "../lib/composerDraft";
import { humanizeGenerationStatus } from "../lib/generationStatus";
import { MessageComposer, type ComposerPhase } from "./MessageComposer";
import { SafeMarkdown } from "./SafeMarkdown";

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
};

type GenerationState = {
  jobId: string;
  threadId: string;
  lastEventId: number;
};

type ScopedError = {
  message: string;
  threadId: string | null;
};

const ACTIVE_JOB_KEY = "cortex.active.generation";
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
}: Props) {
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
  const [partial, setPartial] = useState("");
  const [thoughts, setThoughts] = useState("");
  const [status, setStatus] = useState("Ready");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [generationError, setGenerationError] = useState<ScopedError | null>(null);
  const [activeJob, setActiveJob] = useState<GenerationState | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [loading, setLoading] = useState(true);
  const [forkingMessage, setForkingMessage] = useState<string | null>(null);
  const [attachmentsBusy, setAttachmentsBusy] = useState(false);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const consumingJob = useRef<string | null>(null);
  const startingRef = useRef(false);
  const stoppingRef = useRef(false);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const isNearTranscriptEnd = useRef(true);
  const viewThreadIdRef = useRef<string | null>(threadId);
  const draftsRef = useRef(drafts);
  const attachmentDraftsRef = useRef(attachmentDrafts);

  const loadChat = useCallback(async () => {
    const requestedThreadId = threadId;
    setLoading(true);
    setLoadError(null);
    try {
      const next = requestedThreadId ? await api.chat(requestedThreadId) : null;
      if (viewThreadIdRef.current !== requestedThreadId) return;
      setChat(next);
      setStatus("Ready");
    } catch (requestError) {
      if (viewThreadIdRef.current !== requestedThreadId) return;
      setLoadError(requestError instanceof ApiError ? requestError.detail : "Could not load this chat.");
    } finally {
      if (viewThreadIdRef.current === requestedThreadId) setLoading(false);
    }
  }, [api, threadId]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    const node = transcriptRef.current;
    if (!node) return;
    if (isNearTranscriptEnd.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [chat?.messages?.length, partial, thoughts]);

  const messages = useMemo(() => chat?.messages ?? [], [chat?.messages]);
  const draftScope = composerDraftKey(threadId);
  const draft = drafts[draftScope] ?? readComposerDraft(threadId);
  const attachmentScope = composerAttachmentKey(threadId);
  const attachments = attachmentDrafts[attachmentScope] ?? readComposerAttachments(threadId);
  const finalAssistantId = useMemo(
    () => [...messages].reverse().find((message) => message.role === "assistant")?.id ?? null,
    [messages],
  );
  const displayedThreadId = threadId ?? resolvedThreadId;
  const activeJobForCurrentThread = Boolean(activeJob && activeJob.threadId === displayedThreadId);
  const generationElsewhere = Boolean(activeJob && !activeJobForCurrentThread);
  const visibleGenerationError = generationError && generationError.threadId === displayedThreadId
    ? generationError.message
    : null;
  const composerPhase: ComposerPhase = !runtimeReady
    ? "unavailable"
    : stopping && activeJob
      ? "stopping"
      : activeJob
        ? "generating"
        : starting
          ? "starting"
          : "ready";

  const markIncomingTranscriptActivity = () => {
    if (!isNearTranscriptEnd.current) setShowJumpToLatest(true);
  };

  async function consumeJob(job: GenerationState): Promise<void> {
    if (consumingJob.current === job.jobId) return;
    consumingJob.current = job.jobId;
    const controller = new AbortController();
    abortRef.current = controller;
    let cursor = job.lastEventId;
    let terminal = false;
    setStatus("Connecting to generation...");
    setGenerationError(null);
    try {
      while (!terminal && !controller.signal.aborted) {
        try {
          await api.streamGeneration(job.jobId, (event) => {
            if (event.event_id <= cursor || event.thread_id !== job.threadId) return;
            cursor = event.event_id;
            persistActiveJob({ ...job, lastEventId: cursor });
            setActiveJob({ ...job, lastEventId: cursor });
            const data = event.data ?? {};
            if (typeof data.message === "string") setStatus(data.message);
            if (event.event === "generation.cancelling") setStopping(true);
            if (event.event === "generation.thinking_delta" && typeof data.delta === "string") {
              markIncomingTranscriptActivity();
              setThoughts((current) => current + data.delta);
            }
            if (event.event === "generation.content_delta" && typeof data.delta === "string") {
              markIncomingTranscriptActivity();
              setPartial((current) => current + data.delta);
            }
            if (event.event === "generation.completed") {
              terminal = true;
              void reconcileChat(job.threadId);
            }
            if (event.event === "generation.failed" || event.event === "generation.cancelled") {
              terminal = true;
              setGenerationError({
                threadId: job.threadId,
                message: typeof data.message === "string" ? data.message : "Generation did not complete.",
              });
              void reconcileChat(job.threadId);
            }
          }, { signal: controller.signal, afterEventId: cursor });
          if (!terminal && !controller.signal.aborted) {
            setStatus("Connection interrupted. Reconnecting...");
            await delay(250);
          }
        } catch (streamError) {
          if (controller.signal.aborted) return;
          const snapshot = await api.generationStatus(job.jobId);
          if (snapshot.status === "succeeded" || snapshot.status === "failed" || snapshot.status === "cancelled") {
            terminal = true;
            if (snapshot.status !== "succeeded") {
              setGenerationError({ threadId: job.threadId, message: snapshot.error ?? "Generation did not complete." });
            }
            await reconcileChat(job.threadId);
          } else {
            if (snapshot.status === "cancelling") setStopping(true);
            setStatus("Connection interrupted. Reconnecting...");
            await delay(250);
          }
          if (streamError instanceof ApiError && streamError.status === 401) return;
        }
      }
    } catch (streamError) {
      if (!controller.signal.aborted) {
        setGenerationError({
          threadId: job.threadId,
          message: streamError instanceof Error ? streamError.message : "Generation stream failed.",
        });
      }
    } finally {
      if (terminal) {
        const stored = readActiveJob();
        if (stored?.jobId === job.jobId) clearActiveJob();
        setActiveJob((current) => current?.jobId === job.jobId ? null : current);
        setStopping(false);
        setStatus("Ready");
        setPartial("");
        setThoughts("");
      }
      consumingJob.current = null;
    }
  }

  async function reconcileChat(id: string): Promise<void> {
    try {
      const next = await api.chat(id);
      onChatChanged(next);
      if (viewThreadIdRef.current === id) setChat(next);
    } catch {
      setGenerationError({ threadId: id, message: "Generation finished, but the saved chat could not be reloaded." });
    }
  }

  useEffect(() => {
    viewThreadIdRef.current = threadId;
    const timer = window.setTimeout(() => {
      setResolvedThreadId(threadId);
      void loadChat();
      const stored = readActiveJob();
      if (stored) {
        setActiveJob((current) => current ?? stored);
        void consumeJob(stored);
      } else {
        setActiveJob(null);
        setStopping(false);
        setPartial("");
        setThoughts("");
      }
    }, 0);
    return () => window.clearTimeout(timer);
    // The event consumer intentionally survives route changes. A generation
    // is global to the local backend, while a route is merely a view of it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, loadChat]);

  const startGeneration = async (prompt: string, regenerateMessageId?: string, suppliedAttachments: readonly ChatAttachment[] = attachments): Promise<boolean> => {
    const input = prompt.trim() || (suppliedAttachments.length ? "Please review the attached file(s)." : "");
    if (!input || activeJob || startingRef.current) return false;
    if (!runtimeReady) {
      setGenerationError({
        threadId,
        message: runtimeMessage ?? "The local runtime is unavailable. Rescan local models after it is running.",
      });
      return false;
    }

    startingRef.current = true;
    setStarting(true);
    setLastPrompt(input);
    setLastAttachments([...suppliedAttachments]);
    setGenerationError(null);
    setPartial("");
    setThoughts("");
    try {
      const requestId = createRequestId();
      const accepted = regenerateMessageId
        ? await api.regenerate(threadId ?? "", { request_id: requestId, message_id: regenerateMessageId, user_input: input, attachments: [...suppliedAttachments] })
        : await api.generate({ request_id: requestId, thread_id: threadId, user_input: input, attachments: [...suppliedAttachments], base_revision: chat?.revision ?? 0 });
      const jobThreadId = accepted.thread_id ?? threadId;
      if (!jobThreadId) throw new Error("Cortex did not return a chat thread.");

      setResolvedThreadId(jobThreadId);
      const job: GenerationState = { jobId: accepted.job_id, threadId: jobThreadId, lastEventId: 0 };
      persistActiveJob(job);
      setActiveJob(job);
      if (!regenerateMessageId) {
        setChat((current) => ({
          id: jobThreadId,
          title: current?.title ?? "New Chat",
          timestamp: current?.timestamp ?? new Date().toISOString(),
          revision: (current?.revision ?? 0) + 1,
          messages: accepted.user_message_id && current?.messages?.some((message) => message.id === accepted.user_message_id)
            ? current.messages
            : [...(current?.messages ?? []), { id: accepted.user_message_id ?? undefined, role: "user", content: input, attachments: [...suppliedAttachments] }],
        }));
      }
      if (!threadId) onThreadCreated(jobThreadId);
      void consumeJob(job);
      return true;
    } catch (requestError) {
      setGenerationError({
        threadId,
        message: requestError instanceof ApiError ? requestError.detail : "The response could not be started. Your message is still here.",
      });
      return false;
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  const submitDraft = async (): Promise<boolean> => {
    const submittedDraft = draft;
    const submittedAttachments = attachments;
    const submittedScope = draftScope;
    const submittedThreadId = threadId;
    const accepted = await startGeneration(submittedDraft, undefined, submittedAttachments);
    const currentDraft = draftsRef.current[submittedScope] ?? readComposerDraft(submittedThreadId);
    if (accepted && currentDraft === submittedDraft) {
      const nextDrafts = { ...draftsRef.current, [submittedScope]: "" };
      draftsRef.current = nextDrafts;
      setDrafts(nextDrafts);
      writeComposerDraft(submittedThreadId, "");
      const nextAttachments = { ...attachmentDraftsRef.current, [attachmentScope]: [] };
      attachmentDraftsRef.current = nextAttachments;
      setAttachmentDrafts(nextAttachments);
      writeComposerAttachments(submittedThreadId, []);
    }
    return accepted;
  };

  const cancel = async (): Promise<void> => {
    if (!activeJob || stoppingRef.current) return;
    stoppingRef.current = true;
    setStopping(true);
    setStatus("Stopping response...");
    try {
      await api.cancelGeneration(activeJob.jobId);
    } catch (requestError) {
      setStopping(false);
      setGenerationError({
        threadId: activeJob.threadId,
        message: requestError instanceof ApiError ? requestError.detail : "Could not stop the response.",
      });
    } finally {
      stoppingRef.current = false;
    }
  };

  const retryLastPrompt = async (): Promise<boolean> => {
    if (!lastPrompt) return false;
    return startGeneration(lastPrompt, undefined, lastAttachments);
  };

  const fork = async (message: ChatMessage) => {
    if (!threadId || !message.id || forkingMessage || activeJob || starting) return;
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
      const next = [...attachments, ...staged];
      const nextAttachments = { ...attachmentDraftsRef.current, [attachmentScope]: next };
      attachmentDraftsRef.current = nextAttachments;
      setAttachmentDrafts(nextAttachments);
      writeComposerAttachments(threadId, next);
    } catch (error) {
      setAttachmentError(error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "The attachment could not be uploaded.");
    } finally {
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

  const updateTranscriptPosition = () => {
    const node = transcriptRef.current;
    if (!node) return;
    isNearTranscriptEnd.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
    if (isNearTranscriptEnd.current) setShowJumpToLatest(false);
  };

  const jumpToLatest = () => {
    const node = transcriptRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    isNearTranscriptEnd.current = true;
    setShowJumpToLatest(false);
  };

  if (loading) return <div className="chat-empty-state" aria-live="polite"><span className="loading-spinner" />Loading conversation...</div>;
  if (loadError && !chat) return <div className="chat-empty-state"><h2>Conversation unavailable</h2><p>{loadError}</p><button className="button button-primary" onClick={() => void loadChat()}>Retry</button></div>;

  return (
    <section className="chat-page" aria-labelledby="chat-title">
      <h2 id="chat-title" className="sr-only">{displayChatTitle(chat?.title, "New Chat")}</h2>
      <div className="transcript" ref={transcriptRef} onScroll={updateTranscriptPosition}>
        {messages.length === 0 && !activeJobForCurrentThread && (
          <section className="chat-welcome" aria-label="Start a new conversation">
            <h1>New thread</h1>
            <p>
              Ask a question or check a calculation using the model on this device.
            </p>
            {selectedModel && <span className="chat-welcome-model">{selectedModel}</span>}
          </section>
        )}
        {messages.map((message, index) => (
          <MessageCard
            key={message.id ?? `${message.role}-${index}`}
            message={message}
            isFinalAssistant={message.id === finalAssistantId}
            busy={Boolean(activeJob) || starting}
            onRegenerate={() => void startGeneration(lastPrompt || messages[index - 1]?.content || "", message.id ?? undefined)}
            onFork={() => void fork(message)}
            forking={forkingMessage === message.id}
          />
        ))}
        {activeJobForCurrentThread && !partial && !thoughts && <GenerationStatus status={status} />}
        {activeJobForCurrentThread && (partial || thoughts) && (
          <article className="message-card message-assistant message-pending" aria-label="Cortex response in progress">
            <MessageIdentity role="assistant" />
            <div className="message-bubble">
              {thoughts && <details className="reasoning" open><summary><span>Reasoning</span><span className="disclosure-hint">Live</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={thoughts} /></div></div></details>}
              {partial && <div className="markdown-body"><SafeMarkdown content={partial} /></div>}
              <span className="streaming-caret" aria-hidden="true" />
            </div>
          </article>
        )}
      </div>
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
        />
      </div>
    </section>
  );
}

function GenerationStatus({ status }: { status: string }) {
  return <article className="message-card message-assistant message-pending" aria-label="Cortex response in progress"><MessageIdentity role="assistant" /><div className="message-bubble"><div className="generation-status" role="status"><span className="loading-spinner" aria-hidden="true" />{humanizeGenerationStatus(status)}</div></div></article>;
}

function AttachmentList({ attachments }: { attachments?: ChatAttachment[] | null }) {
  if (!attachments?.length) return null;
  return (
    <div className="message-attachments" aria-label="Attached files">
      {attachments.map((attachment) => (
        <span className="message-attachment" key={attachment.attachment_id} title={attachment.filename}>
          {attachment.kind === "image" ? <ImageIcon size={14} aria-hidden="true" /> : <FileText size={14} aria-hidden="true" />}
          <span className="message-attachment-name">{attachment.filename}</span>
        </span>
      ))}
    </div>
  );
}

function MessageCard({ message, isFinalAssistant, busy, onRegenerate, onFork, forking }: { message: ChatMessage; isFinalAssistant: boolean; busy: boolean; onRegenerate: () => void; onFork: () => void; forking: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <article className={`message-card message-${message.role}`} aria-label={`${message.role === "assistant" ? "Cortex" : message.role === "user" ? "Your" : "System"} message`}>
      <MessageIdentity role={message.role} timestamp={message.timestamp} />
      <div className="message-bubble">
        <AttachmentList attachments={message.attachments} />
        {message.thoughts && <details className="reasoning"><summary><span>Reasoning</span><span className="disclosure-hint">Show details</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.thoughts} /></div></div></details>}
        <div className="markdown-body">{message.role === "user" ? <p>{message.content}</p> : <SafeMarkdown content={message.content} />}</div>
        {message.sources && message.sources.length > 0 && <details className="sources"><summary><span>Sources</span><span className="disclosure-hint">{message.sources.length} {message.sources.length === 1 ? "item" : "items"}</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.sources.map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("\n\n")} /></div></div></details>}
      </div>
      <div className="message-actions" aria-label="Message actions">
        <button className="icon-button icon-button-small" type="button" aria-label={copied ? "Message copied" : "Copy message"} title={copied ? "Copied" : "Copy message"} onClick={() => void copy()}><Copy size={14} aria-hidden="true" />{copied && <span className="message-action-feedback">Copied</span>}</button>
        {message.role === "assistant" && <>
          <button className="icon-button icon-button-small" type="button" aria-label="Regenerate response" title="Regenerate response" disabled={!isFinalAssistant || busy} onClick={onRegenerate}><RefreshCw size={14} aria-hidden="true" /></button>
          <button className="icon-button icon-button-small" type="button" aria-label="Fork chat from this message" title="Fork chat from this message" disabled={busy || forking || !message.id} onClick={onFork}><GitBranch size={14} aria-hidden="true" /></button>
        </>}
      </div>
    </article>
  );
}

function MessageIdentity({ role, timestamp }: { role: ChatMessage["role"]; timestamp?: string | null }) {
  const label = role === "assistant" ? "Cortex" : role === "user" ? "You" : "System";
  const mark = role === "assistant" ? "C" : role === "user" ? "Y" : "S";
  const displayTime = formatMessageTime(timestamp);
  return <div className="message-meta"><span className={`message-author-mark message-author-mark-${role}`} aria-hidden="true">{mark}</span><span className="message-author">{label}</span>{displayTime && <time dateTime={timestamp ?? undefined}>{displayTime}</time>}</div>;
}

function formatMessageTime(value?: string | null): string | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return null;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(timestamp);
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

function readActiveJob(): GenerationState | null {
  const raw = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  if (!raw) return null;
  try { return JSON.parse(raw) as GenerationState; } catch { return null; }
}

function persistActiveJob(job: GenerationState): void { window.sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job)); }
function clearActiveJob(): void { window.sessionStorage.removeItem(ACTIVE_JOB_KEY); }
function delay(milliseconds: number): Promise<void> { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }
