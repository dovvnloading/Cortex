import { create } from "zustand";
import type { ChatGroup, ChatResponse, ChatSummary, GenerationOptionsOverride } from "../../../contracts/cortex-api";

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
  /** User-created folders/projects, in their persisted display order. */
  groups: ChatGroup[];
  generation: GenerationState;
  /**
   * Sequence number of the last SSE event applied to the tracked generation.
   *
   * Deliberately a sibling of `generation` rather than a field inside it.
   * Nothing renders this -- only ChatPage's resume effect reads it, and it
   * does so imperatively via getState(). It does advance on every single SSE
   * frame, so while it lived inside `generation` it rebuilt that object per
   * frame, and ChatPage subscribes to the object by reference: the transcript
   * re-rendered on every frame for a value it never draws, cancelling out the
   * requestAnimationFrame batching in useGenerationStream.
   */
  generationCursor: number;
  generationOptionsByThread: Record<string, GenerationOptionsOverride>;

  setChats: (next: ChatSummary[] | ((current: ChatSummary[]) => ChatSummary[])) => void;
  upsertChatSummary: (chat: ChatResponse) => void;

  setGroups: (next: ChatGroup[] | ((current: ChatGroup[]) => ChatGroup[])) => void;
  /** Replace one group in place, keeping its position in the list. */
  upsertGroup: (group: ChatGroup) => void;
  /** Drop the group and return every chat filed under it to ungrouped. */
  removeGroup: (groupId: string) => void;
  setChatGroup: (threadId: string, groupId: string | null) => void;

  beginGeneration: (jobId: string, threadId: string) => void;
  appendContentToken: (jobId: string, delta: string) => void;
  appendThinkingToken: (jobId: string, delta: string) => void;
  setGenerationCursor: (jobId: string, eventId: number) => void;
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
  groups: [],
  generation: idleGeneration,
  generationCursor: 0,
  generationOptionsByThread: {},

  setChats: (next) =>
    set((state) => ({ chats: typeof next === "function" ? (next as (current: ChatSummary[]) => ChatSummary[])(state.chats) : next })),
  upsertChatSummary: (chat) =>
    set((state) => ({
      chats: [
        // Preserve the filing: a chat that gains a message must not jump out
        // of its group just because the summary was rebuilt from the
        // response, which carries group_id only once the server has it.
        {
          id: chat.id,
          title: chat.title,
          timestamp: chat.timestamp,
          group_id: chat.group_id ?? state.chats.find((item) => item.id === chat.id)?.group_id ?? null,
        },
        ...state.chats.filter((item) => item.id !== chat.id),
      ],
    })),

  setGroups: (next) =>
    set((state) => ({ groups: typeof next === "function" ? (next as (current: ChatGroup[]) => ChatGroup[])(state.groups) : next })),
  upsertGroup: (group) =>
    set((state) => (
      state.groups.some((item) => item.id === group.id)
        ? { groups: state.groups.map((item) => (item.id === group.id ? group : item)) }
        : { groups: [...state.groups, group] }
    )),
  removeGroup: (groupId) =>
    set((state) => ({
      groups: state.groups.filter((group) => group.id !== groupId),
      // Mirrors the server: deleting a group never deletes its chats.
      chats: state.chats.map((chat) => (chat.group_id === groupId ? { ...chat, group_id: null } : chat)),
    })),
  setChatGroup: (threadId, groupId) =>
    set((state) => ({
      chats: state.chats.map((chat) => (chat.id === threadId ? { ...chat, group_id: groupId } : chat)),
    })),

  beginGeneration: (jobId, threadId) =>
    set({ generation: { ...idleGeneration, jobId, threadId, phase: "starting" }, generationCursor: 0 }),
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
  // The two reducers below run on *every* SSE frame, and both used to build a
  // fresh generation object whether or not anything had changed. ChatPage
  // subscribes to that object by reference, so an unchanged rewrite still
  // re-rendered the whole transcript -- which is exactly what the rAF batching
  // in useGenerationStream exists to avoid. Returning `state` untouched when
  // the value has not moved is what makes that batching effective.
  setGenerationCursor: (jobId, eventId) =>
    set((state) => (
      state.generation.jobId === jobId && eventId > state.generationCursor
        ? { generationCursor: eventId }
        : state
    )),
  setStatusText: (jobId, text) =>
    set((state) => (
      state.generation.jobId === jobId && text !== state.generation.statusText
        ? { generation: { ...state.generation, statusText: text } }
        : state
    )),
  markContentReady: (jobId) =>
    set((state) => (
      state.generation.jobId === jobId && !state.generation.contentReady
        ? { generation: { ...state.generation, contentReady: true } }
        : state
    )),
  markStopping: (jobId) =>
    set((state) => (
      state.generation.jobId === jobId && state.generation.phase !== "stopping"
        ? { generation: { ...state.generation, phase: "stopping" } }
        : state
    )),
  revertStopping: (jobId) =>
    set((state) =>
      state.generation.jobId === jobId && state.generation.phase === "stopping"
        ? { generation: { ...state.generation, phase: "streaming" } }
        : state,
    ),
  endGeneration: (jobId) =>
    set((state) => (
      state.generation.jobId === jobId ? { generation: idleGeneration, generationCursor: 0 } : state
    )),

  setThreadOptions: (threadKey, options) =>
    set((state) => {
      const next = { ...state.generationOptionsByThread };
      if (options === null) delete next[threadKey];
      else next[threadKey] = options;
      return { generationOptionsByThread: next };
    }),
}));
