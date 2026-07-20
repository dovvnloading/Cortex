import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Copy, GitBranch, RefreshCw, Send, ShieldCheck, Sparkles, Square } from "lucide-react";
import type { ChatMessage, ChatResponse } from "../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../api/client";
import { SafeMarkdown } from "./SafeMarkdown";

type Props = {
  api: CortexApi;
  threadId: string | null;
  activeModel: string | null;
  runtimeReady: boolean;
  runtimeMessage: string | null;
  onThreadCreated: (threadId: string) => void;
  onChatChanged: (chat: ChatResponse) => void;
  onForked: (chat: ChatResponse) => void;
};

type GenerationState = {
  jobId: string;
  threadId: string;
  lastEventId: number;
};

const ACTIVE_JOB_KEY = "cortex.active.generation";

export function ChatPage({ api, threadId, activeModel, runtimeReady, runtimeMessage, onThreadCreated, onChatChanged, onForked }: Props) {
  const [chat, setChat] = useState<ChatResponse | null>(null);
  const [resolvedThreadId, setResolvedThreadId] = useState<string | null>(threadId);
  const [draft, setDraft] = useState("");
  const [lastPrompt, setLastPrompt] = useState("");
  const [partial, setPartial] = useState("");
  const [thoughts, setThoughts] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [status, setStatus] = useState("Ready");
  const [error, setError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<GenerationState | null>(null);
  const [loading, setLoading] = useState(true);
  const [forkingMessage, setForkingMessage] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const consumingJob = useRef<string | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const loadChat = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = threadId ? await api.chat(threadId) : null;
      setChat(next);
      setStatus("Ready");
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.detail : "Could not load this chat.");
    } finally {
      setLoading(false);
    }
  }, [api, threadId]);

  useEffect(() => {
    abortRef.current?.abort();
    consumingJob.current = null;
    const timer = window.setTimeout(() => {
      void loadChat();
      setResolvedThreadId(threadId);
      const stored = readActiveJob();
      if (stored && stored.threadId === threadId) {
        setActiveJob(stored);
        void consumeJob(stored);
      } else {
        setActiveJob(null);
        setPartial("");
        setThoughts("");
      }
      const pendingSuggestions = threadId ? readPendingSuggestions(threadId) : [];
      if (pendingSuggestions.length) {
        setSuggestions(pendingSuggestions);
        clearPendingSuggestions(threadId as string);
      } else setSuggestions([]);
    }, 0);
    return () => window.clearTimeout(timer);
  // The stream callback is intentionally stable through refs; route changes own this lifecycle.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, loadChat]);

  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [chat?.messages?.length, partial, thoughts]);

  useEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    composer.style.height = "0px";
    const nextHeight = Math.min(Math.max(composer.scrollHeight, 52), 184);
    composer.style.height = `${nextHeight}px`;
    composer.style.overflowY = composer.scrollHeight > 184 ? "auto" : "hidden";
  }, [draft]);

  const messages = useMemo(() => chat?.messages ?? [], [chat?.messages]);
  const finalAssistantId = useMemo(
    () => [...messages].reverse().find((message) => message.role === "assistant")?.id ?? null,
    [messages],
  );

  async function consumeJob(job: GenerationState): Promise<void> {
    if (consumingJob.current === job.jobId) return;
    consumingJob.current = job.jobId;
    const controller = new AbortController();
    abortRef.current = controller;
    let cursor = job.lastEventId;
    let terminal = false;
    setStatus("Connecting to generation...");
    setError(null);
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
            if (event.event === "generation.thinking_delta" && typeof data.delta === "string") {
              setThoughts((current) => current + data.delta);
            }
            if (event.event === "generation.content_delta" && typeof data.delta === "string") {
              setPartial((current) => current + data.delta);
            }
            if (event.event === "generation.completed") {
              terminal = true;
              const nextSuggestions = Array.isArray(data.suggestions) ? data.suggestions.filter((value: unknown): value is string => typeof value === "string") : [];
              setSuggestions(nextSuggestions);
              persistPendingSuggestions(job.threadId, nextSuggestions);
              void reconcileChat(job.threadId).then(() => {
                if (!threadId) onThreadCreated(job.threadId);
              });
            }
            if (event.event === "generation.failed" || event.event === "generation.cancelled") {
              terminal = true;
              setError(typeof data.message === "string" ? data.message : "Generation did not complete.");
              void reconcileChat(job.threadId);
            }
          }, { signal: controller.signal, afterEventId: cursor });
        } catch (streamError) {
          if (controller.signal.aborted) return;
          const snapshot = await api.generationStatus(job.jobId);
          if (snapshot.status === "succeeded" || snapshot.status === "failed" || snapshot.status === "cancelled") {
            terminal = true;
            if (snapshot.status !== "succeeded") setError(snapshot.error ?? "Generation did not complete.");
            await reconcileChat(job.threadId);
          } else {
            setStatus("Connection interrupted. Reconnecting...");
            await delay(250);
          }
          if (streamError instanceof ApiError && streamError.status === 401) return;
        }
      }
    } catch (streamError) {
      if (!controller.signal.aborted) setError(streamError instanceof Error ? streamError.message : "Generation stream failed.");
    } finally {
      if (terminal) {
        clearActiveJob();
        setActiveJob(null);
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
      setChat(next);
      onChatChanged(next);
    } catch {
      setError("Generation finished, but the saved chat could not be reloaded.");
    }
  }

  const startGeneration = async (prompt: string, regenerateMessageId?: string) => {
    const input = prompt.trim();
    if (!input || activeJob) return;
    if (!runtimeReady) {
      setError(runtimeMessage ?? "Ollama is unavailable. Start it, then rescan Models in Settings.");
      return;
    }
    setLastPrompt(input);
    setError(null);
    setPartial("");
    setThoughts("");
    setSuggestions([]);
    try {
      const requestId = createRequestId();
      const currentThreadId = resolvedThreadId ?? threadId;
      const accepted = regenerateMessageId
        ? await api.regenerate(currentThreadId ?? "", { request_id: requestId, message_id: regenerateMessageId, user_input: input })
        : await api.generate({ request_id: requestId, thread_id: currentThreadId, user_input: input, base_revision: chat?.revision ?? 0 });
      const jobThreadId = accepted.thread_id ?? currentThreadId;
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
            : [...(current?.messages ?? []), { id: accepted.user_message_id ?? undefined, role: "user", content: input }],
        }));
      }
      void consumeJob(job);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.detail : "Could not start generation.");
    }
  };

  const send = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const input = draft;
    setDraft("");
    void startGeneration(input);
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const choosePrompt = (prompt: string) => {
    setDraft(prompt);
    window.requestAnimationFrame(() => composerRef.current?.focus());
  };

  const cancel = async () => {
    if (!activeJob) return;
    try {
      await api.cancelGeneration(activeJob.jobId);
      setStatus("Cancelling...");
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.detail : "Could not cancel generation.");
    }
  };

  const fork = async (message: ChatMessage) => {
    if (!threadId || !message.id || forkingMessage) return;
    setForkingMessage(message.id);
    try {
      const forked = await api.forkChat(threadId, message.id);
      onForked(forked);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.detail : "Could not fork this chat.");
    } finally {
      setForkingMessage(null);
    }
  };

  const composerDisabled = Boolean(activeJob) || !runtimeReady;

  if (loading) return <div className="chat-empty-state" aria-live="polite"><span className="loading-spinner" />Loading conversation...</div>;
  if (error && !chat) return <div className="chat-empty-state"><h2>Conversation unavailable</h2><p>{error}</p><button className="button button-primary" onClick={() => void loadChat()}>Retry</button></div>;

  return (
    <section className="chat-page" aria-labelledby="chat-title">
      <h2 id="chat-title" className="sr-only">{chat?.title ?? "New Chat"}</h2>
      <div className="transcript" ref={transcriptRef} aria-live="polite">
        {!messages.length && !partial && <EmptyChatState activeModel={activeModel} runtimeReady={runtimeReady} runtimeMessage={runtimeMessage} onChoosePrompt={choosePrompt} />}
        {messages.map((message, index) => <MessageCard key={message.id ?? `${message.role}-${index}`} message={message} isFinalAssistant={message.id === finalAssistantId} busy={Boolean(activeJob)} onRegenerate={() => void startGeneration(lastPrompt || messages[index - 1]?.content || "", message.id ?? undefined)} onFork={() => void fork(message)} forking={forkingMessage === message.id} />)}
        {activeJob && (partial || thoughts || status) && <article className="message-card message-assistant message-pending" aria-label="Cortex response in progress"><div className="message-bubble"><div className="loading-label">{status}</div>{thoughts && <details className="reasoning"><summary>Reasoning</summary><SafeMarkdown content={thoughts} /></details>}{partial && <div className="markdown-body"><SafeMarkdown content={partial} /></div>}<span className="streaming-caret" aria-hidden="true" /></div></article>}
      </div>
      {suggestions.length > 0 && !activeJob && <div className="suggestions" aria-label="Follow-up suggestions">{suggestions.map((suggestion) => <button className="suggestion-chip" key={suggestion} onClick={() => choosePrompt(suggestion)}>{suggestion}</button>)}</div>}
      {error && <div className="chat-error" role="alert"><span>{error}</span><button className="button button-quiet" onClick={() => void startGeneration(lastPrompt)}>Retry</button></div>}
      <div className="input-container">
        <form className="composer" onSubmit={send}>
          <label className="sr-only" htmlFor="chat-composer">Message Cortex</label>
          <textarea ref={composerRef} id="chat-composer" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={handleComposerKeyDown} disabled={composerDisabled} placeholder={runtimeReady ? "Ask a question..." : "Start Ollama to continue"} rows={1} />
          {activeJob
            ? <button className="button button-danger composer-submit" type="button" onClick={() => void cancel()}><Square size={15} aria-hidden="true" /> Cancel</button>
            : <button className="button button-primary composer-submit" type="submit" disabled={!draft.trim() || !runtimeReady}><Send size={15} aria-hidden="true" /> Send</button>}
        </form>
        <div className="composer-meta">
          <span className={`composer-runtime ${runtimeReady ? "composer-runtime-ready" : "composer-runtime-error"}`}><span className="composer-runtime-dot" aria-hidden="true" />{runtimeReady ? <><strong>Local model</strong> {activeModel ?? "Ready"}</> : "Ollama unavailable"}</span>
          <span className="composer-shortcut"><kbd>Enter</kbd> to send <span aria-hidden="true">&middot;</span> <kbd>Shift + Enter</kbd> for a new line</span>
        </div>
      </div>
    </section>
  );
}

function EmptyChatState({ activeModel, runtimeReady, runtimeMessage, onChoosePrompt }: { activeModel: string | null; runtimeReady: boolean; runtimeMessage: string | null; onChoosePrompt: (prompt: string) => void }) {
  const prompts = [
    "Help me plan a focused day.",
    "Explain a complex idea clearly.",
    "Turn my rough notes into a polished draft.",
  ];
  return (
    <div className="chat-empty-state chat-launchpad">
      <div className="chat-launchpad-mark" aria-hidden="true"><img src="/cortex.svg" alt="" /></div>
      <p className="eyebrow">LOCAL, PRIVATE, YOURS</p>
      <h3>{runtimeReady ? "What will you make today?" : "Your local runtime is offline"}</h3>
      <p>{runtimeReady ? `Cortex is ready on this computer${activeModel ? ` with ${activeModel}` : ""}. Start with an idea, a question, or a rough draft.` : runtimeMessage ?? "Start Ollama, then rescan Models in Settings to continue."}</p>
      {runtimeReady && <div className="launchpad-prompts" aria-label="Conversation starters">
        {prompts.map((prompt) => <button className="launchpad-prompt" type="button" key={prompt} onClick={() => onChoosePrompt(prompt)}><Sparkles aria-hidden="true" size={15} /><span>{prompt}</span></button>)}
      </div>}
      <div className="launchpad-privacy"><ShieldCheck aria-hidden="true" size={15} /> Your conversations stay in your local Cortex workspace.</div>
    </div>
  );
}

function MessageCard({ message, isFinalAssistant, busy, onRegenerate, onFork, forking }: { message: ChatMessage; isFinalAssistant: boolean; busy: boolean; onRegenerate: () => void; onFork: () => void; forking: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!navigator.clipboard) return;
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return <article className={`message-card message-${message.role}`}><div className="message-bubble">{message.thoughts && <details className="reasoning"><summary>Reasoning</summary><SafeMarkdown content={message.thoughts} /></details>}<div className="markdown-body">{message.role === "user" ? <p>{message.content}</p> : <SafeMarkdown content={message.content} />}</div>{message.sources && message.sources.length > 0 && <details className="sources"><summary>Sources</summary><SafeMarkdown content={message.sources.map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("\n\n")} /></details>}</div><div className="message-actions"><button className="icon-button icon-button-small" type="button" aria-label="Copy message" onClick={() => void copy()}><Copy size={14} aria-hidden="true" />{copied && <span className="sr-only">Copied</span>}</button>{message.role === "assistant" && <><button className="icon-button icon-button-small" type="button" aria-label="Regenerate response" disabled={!isFinalAssistant || busy} onClick={onRegenerate}><RefreshCw size={14} aria-hidden="true" /></button><button className="icon-button icon-button-small" type="button" aria-label="Fork chat from this message" disabled={busy || forking || !message.id} onClick={onFork}><GitBranch size={14} aria-hidden="true" /></button></>}</div></article>;
}

function createRequestId(): string {
  return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

function readActiveJob(): GenerationState | null {
  const raw = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  if (!raw) return null;
  try { return JSON.parse(raw) as GenerationState; } catch { return null; }
}

function persistActiveJob(job: GenerationState): void { window.sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job)); }
function clearActiveJob(): void { window.sessionStorage.removeItem(ACTIVE_JOB_KEY); }
function pendingSuggestionsKey(threadId: string): string { return `cortex.pending.suggestions.${threadId}`; }
function persistPendingSuggestions(threadId: string, suggestions: string[]): void { if (suggestions.length) window.sessionStorage.setItem(pendingSuggestionsKey(threadId), JSON.stringify(suggestions)); }
function readPendingSuggestions(threadId: string): string[] { try { const value = JSON.parse(window.sessionStorage.getItem(pendingSuggestionsKey(threadId)) ?? "[]"); return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; } catch { return []; } }
function clearPendingSuggestions(threadId: string): void { window.sessionStorage.removeItem(pendingSuggestionsKey(threadId)); }
function delay(milliseconds: number): Promise<void> { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }
