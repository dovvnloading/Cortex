import { create } from "zustand";
import type { ChatResponse, ChatSummary, GenerationOptionsOverride } from "../../../contracts/cortex-api";

/** A brand-new, not-yet-created chat has no thread id yet; scope its draft options under this key. */
export const NEW_THREAD_OPTIONS_KEY = "new";

export type GenerationPhase = "idle" | "starting" | "streaming" | "stopping";

export interface GenerationState {
  jobId: string | null;
  threadId: string | null;
  phase: GenerationPhase;
  partialContent: string;
  partialThoughts: string;
  statusText: string;
  /**
   * True once the backend has sent every content/thinking token and moved
   * on to bookkeeping (persisting the message, generating a chat title).
   * The answer text itself never changes after this point, even though the
   * job is technically still "running" for a bit longer -- title
   * generation in particular can take as long as the answer itself for a
   * reasoning model, so the UI must stop looking like it's still typing
   * once this flips, rather than hanging on a blinking cursor for
   * unrelated backend bookkeeping the user can't see the effect of yet.
   */
  contentReady: boolean;
}

interface ChatStoreState {
  chats: ChatSummary[];
  activeChat: ChatResponse | null;
  generation: GenerationState;
  generationOptionsByThread: Record<string, GenerationOptionsOverride>;

  setChats: (next: ChatSummary[] | ((current: ChatSummary[]) => ChatSummary[])) => void;
  upsertChatSummary: (chat: ChatResponse) => void;
  setActiveChat: (chat: ChatResponse | null) => void;

  beginGeneration: (jobId: string, threadId: string) => void;
  appendContentToken: (jobId: string, delta: string) => void;
  appendThinkingToken: (jobId: string, delta: string) => void;
  setStatusText: (jobId: string, text: string) => void;
  markContentReady: (jobId: string) => void;
  markStopping: (jobId: string) => void;
  revertStopping: (jobId: string) => void;
  endGeneration: (jobId: string) => void;

  setThreadOptions: (threadKey: string, options: GenerationOptionsOverride | null) => void;
}

const idleGeneration: GenerationState = {
  jobId: null,
  threadId: null,
  phase: "idle",
  partialContent: "",
  partialThoughts: "",
  statusText: "",
  contentReady: false,
};

/**
 * Chat-list summaries and the active generation stream. Generation is
 * intentionally global (not scoped to one ChatPage instance) so it survives
 * a route change to Settings and back, and so "generating in another
 * thread" reads correctly regardless of which ChatPage props are current.
 * Error handling for a generation stays page-local (see useGenerationStream
 * / ChatPage) since it's tightly coupled to which thread is being viewed.
 */
export const useChatStore = create<ChatStoreState>((set) => ({
  chats: [],
  activeChat: null,
  generation: idleGeneration,
  generationOptionsByThread: {},

  setChats: (next) =>
    set((state) => ({ chats: typeof next === "function" ? (next as (current: ChatSummary[]) => ChatSummary[])(state.chats) : next })),
  upsertChatSummary: (chat) =>
    set((state) => ({
      chats: [
        { id: chat.id, title: chat.title, timestamp: chat.timestamp },
        ...state.chats.filter((item) => item.id !== chat.id),
      ],
    })),
  setActiveChat: (chat) => set({ activeChat: chat }),

  beginGeneration: (jobId, threadId) =>
    set({ generation: { ...idleGeneration, jobId, threadId, phase: "starting" } }),
  appendContentToken: (jobId, delta) =>
    set((state) =>
      state.generation.jobId === jobId
        ? { generation: { ...state.generation, phase: "streaming", partialContent: state.generation.partialContent + delta } }
        : state,
    ),
  appendThinkingToken: (jobId, delta) =>
    set((state) =>
      state.generation.jobId === jobId
        ? { generation: { ...state.generation, phase: "streaming", partialThoughts: state.generation.partialThoughts + delta } }
        : state,
    ),
  setStatusText: (jobId, text) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...state.generation, statusText: text } } : state)),
  markContentReady: (jobId) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...state.generation, contentReady: true } } : state)),
  markStopping: (jobId) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...state.generation, phase: "stopping" } } : state)),
  revertStopping: (jobId) =>
    set((state) =>
      state.generation.jobId === jobId && state.generation.phase === "stopping"
        ? { generation: { ...state.generation, phase: "streaming" } }
        : state,
    ),
  endGeneration: (jobId) => set((state) => (state.generation.jobId === jobId ? { generation: idleGeneration } : state)),

  setThreadOptions: (threadKey, options) =>
    set((state) => {
      const next = { ...state.generationOptionsByThread };
      if (options === null) delete next[threadKey];
      else next[threadKey] = options;
      return { generationOptionsByThread: next };
    }),
}));
