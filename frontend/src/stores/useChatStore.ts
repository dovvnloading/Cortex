import { create } from "zustand";
import type { ChatGroup, ChatResponse, ChatSummary, GenerationOptionsOverride } from "../../../contracts/cortex-api";
import { GENERATION_GAP_NOTICE } from "../lib/generationStatus";

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
  /**
   * True once events of this job were skipped: the stream came back after the
   * backend had already dropped the older ones, so the text held here has a
   * hole in it. Nothing more is appended after that; the saved answer replaces
   * the bubble when the job ends.
   */
  gap: boolean;
}

/**
 * How a generation ended, published by the stream consumer for whichever view
 * is showing the chat. `id` only ever grows, so a reader can tell a new outcome
 * from one it already handled, and acknowledges an outcome to take it off the
 * store -- which is what lets one that arrived while no chat was on screen (for
 * example Settings was open) wait for the next chat view instead of being lost.
 */
export interface GenerationCompletion {
  id: number;
  jobId: string | null;
  threadId: string;
  /** The saved chat after the job, or null when reloading it failed. */
  chat: ChatResponse | null;
  clearRequested: boolean;
}

export interface GenerationFailure {
  id: number;
  threadId: string;
  message: string;
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
   * Nothing renders this -- only GenerationStreamHost's resume reads it, and it
   * does so imperatively via getState(). It does advance on every single SSE
   * frame, so while it lived inside `generation` it rebuilt that object per
   * frame, and ChatPage subscribes to the object by reference: the transcript
   * re-rendered on every frame for a value it never draws, cancelling out the
   * requestAnimationFrame batching in useGenerationStream.
   */
  generationCursor: number;
  /** The newest outcome nobody has acknowledged yet; see {@link GenerationCompletion}. */
  lastCompletion: GenerationCompletion | null;
  lastFailure: GenerationFailure | null;
  generationOptionsByThread: Record<string, GenerationOptionsOverride>;
  /**
   * Assistant messages whose requested translation failed, so the original
   * answer is shown instead. The backend reports this only on the job
   * result, not on the stored message, so the note lasts for this session.
   */
  untranslatedMessageIds: Record<string, true>;
  /**
   * Memories the model suggested, by the assistant message they were made
   * under. A suggestion is only a suggestion: nothing is stored until the user
   * saves it from the card. The backend reports it on the job, not on the
   * stored message, so like the note above it lasts for this session.
   */
  proposedMemoriesByMessage: Record<string, readonly string[]>;

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
  /** Swap the whole visible answer, e.g. for its translation after the original was streamed. */
  replaceContent: (jobId: string, content: string) => void;
  setGenerationCursor: (jobId: string, eventId: number) => void;
  setStatusText: (jobId: string, text: string) => void;
  markContentReady: (jobId: string) => void;
  markStopping: (jobId: string) => void;
  revertStopping: (jobId: string) => void;
  /** Record that events were skipped, and say so in the status line. */
  markGenerationGap: (jobId: string) => void;
  endGeneration: (jobId: string) => void;
  recordCompletion: (completion: Omit<GenerationCompletion, "id">) => void;
  acknowledgeCompletion: (id: number) => void;
  recordFailure: (failure: Omit<GenerationFailure, "id">) => void;
  acknowledgeFailure: (id: number) => void;

  setThreadOptions: (threadKey: string, options: GenerationOptionsOverride | null) => void;
  markUntranslated: (messageId: string) => void;
  /** Replace a message's pending suggestions; an empty list forgets them (a regenerated answer has new ones). */
  setProposedMemories: (messageId: string, memos: readonly string[]) => void;
  /** Drop one suggestion, whether it was saved or declined. */
  dismissProposedMemory: (messageId: string, memo: string) => void;
}

const idleGeneration: GenerationState = {
  jobId: null,
  threadId: null,
  phase: "idle",
  partialContent: "",
  partialThoughts: "",
  statusText: "",
  contentReady: false,
  gap: false,
};

let nextOutcomeId = 0;

/**
 * Chat-list summaries and the active generation stream. Generation is
 * intentionally global (not scoped to one ChatPage instance) so it survives
 * a route change to Settings and back, and so "generating in another
 * thread" reads correctly regardless of which ChatPage props are current.
 * The stream itself is consumed above the routes (GenerationStreamHost); how a
 * job ended is published here as lastCompletion / lastFailure, and the chat
 * view reacts to it, since what to show is tied to which thread is on screen.
 */
export const useChatStore = create<ChatStoreState>((set) => ({
  chats: [],
  groups: [],
  generation: idleGeneration,
  generationCursor: 0,
  lastCompletion: null,
  lastFailure: null,
  generationOptionsByThread: {},
  untranslatedMessageIds: {},
  proposedMemoriesByMessage: {},

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
  replaceContent: (jobId, content) =>
    set((state) =>
      state.generation.jobId === jobId && content !== state.generation.partialContent
        ? { generation: { ...state.generation, phase: "streaming", partialContent: content } }
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
  markGenerationGap: (jobId) =>
    set((state) => (
      state.generation.jobId === jobId && !state.generation.gap
        ? { generation: { ...state.generation, gap: true, statusText: GENERATION_GAP_NOTICE } }
        : state
    )),
  endGeneration: (jobId) =>
    set((state) => (
      state.generation.jobId === jobId ? { generation: idleGeneration, generationCursor: 0 } : state
    )),
  recordCompletion: (completion) =>
    set({ lastCompletion: { ...completion, id: ++nextOutcomeId } }),
  acknowledgeCompletion: (id) =>
    set((state) => (state.lastCompletion?.id === id ? { lastCompletion: null } : state)),
  recordFailure: (failure) =>
    set({ lastFailure: { ...failure, id: ++nextOutcomeId } }),
  acknowledgeFailure: (id) =>
    set((state) => (state.lastFailure?.id === id ? { lastFailure: null } : state)),

  setThreadOptions: (threadKey, options) =>
    set((state) => {
      const next = { ...state.generationOptionsByThread };
      if (options === null) delete next[threadKey];
      else next[threadKey] = options;
      return { generationOptionsByThread: next };
    }),
  markUntranslated: (messageId) =>
    set((state) => (
      state.untranslatedMessageIds[messageId]
        ? state
        : { untranslatedMessageIds: { ...state.untranslatedMessageIds, [messageId]: true } }
    )),
  setProposedMemories: (messageId, memos) =>
    set((state) => {
      const current = state.proposedMemoriesByMessage[messageId];
      if (memos.length === 0) {
        if (!current) return state;
        const next = { ...state.proposedMemoriesByMessage };
        delete next[messageId];
        return { proposedMemoriesByMessage: next };
      }
      if (current && current.length === memos.length && current.every((memo, index) => memo === memos[index])) return state;
      return { proposedMemoriesByMessage: { ...state.proposedMemoriesByMessage, [messageId]: [...memos] } };
    }),
  dismissProposedMemory: (messageId, memo) =>
    set((state) => {
      const current = state.proposedMemoriesByMessage[messageId];
      if (!current?.includes(memo)) return state;
      const remaining = current.filter((item) => item !== memo);
      const next = { ...state.proposedMemoriesByMessage };
      if (remaining.length > 0) next[messageId] = remaining;
      else delete next[messageId];
      return { proposedMemoriesByMessage: next };
    }),
}));
