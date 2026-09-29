import { act, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import { ApiError, type CortexApi } from "../../api/client";
import { readActiveJob, trackGeneration } from "../../hooks/useGenerationStream";
import { useChatStore } from "../../stores/useChatStore";
import { GenerationStreamHost } from "./GenerationStreamHost";

type Emit = (event: { event_id: number; event: string; data?: Record<string, unknown> }) => void;

const savedChat = (threadId: string): ChatResponse => ({
  id: threadId,
  title: "Saved title",
  timestamp: "2026-01-01T00:00:00Z",
  revision: 2,
  messages: [{ id: "answer-1", role: "assistant", content: "Saved answer" }],
});

/** A fake API whose stream the test feeds by hand; each call records its cursor and signal. */
function fakeStreamApi(overrides: Partial<CortexApi> = {}) {
  const attachments: Array<{ jobId: string; afterEventId?: number; signal?: AbortSignal; emit: Emit }> = [];
  const streamGeneration = vi.fn((jobId: string, onEvent: (event: unknown) => void, options: { signal?: AbortSignal; afterEventId?: number } = {}) =>
    new Promise<void>((resolve, reject) => {
      const emit: Emit = (event) => {
        onEvent({ job_id: jobId, thread_id: "thread-a", data: {}, ...event });
        if (["generation.completed", "generation.failed", "generation.cancelled"].includes(event.event)) resolve();
      };
      attachments.push({ jobId, afterEventId: options.afterEventId, signal: options.signal, emit });
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }));
  const api = {
    chat: vi.fn(async (id: string) => savedChat(id)),
    generationStatus: vi.fn(),
    streamGeneration,
    ...overrides,
  } as unknown as CortexApi;
  return { api, attachments, streamGeneration };
}

const expireSession = () => undefined;

/**
 * A zero-delay timer runs after every timer already queued, and the host only
 * ever attaches from one, so this is a barrier for "nothing further happened"
 * checks. It is not a wall-clock wait: never use it to give an effect time to
 * show up.
 */
const settleTimers = () => act(async () => {
  await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
});

describe("GenerationStreamHost", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    const { generation, endGeneration } = useChatStore.getState();
    if (generation.jobId) endGeneration(generation.jobId);
  });

  it("attaches to a job a page starts tracking, from its first event", async () => {
    const { api, attachments } = fakeStreamApi();
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);

    act(() => trackGeneration("job-1", "thread-a"));

    // In the same turn, not a tick later: the stream opens as early as it did
    // when the page owned it, so a fast job cannot finish before it is followed.
    expect(attachments).toHaveLength(1);
    expect(attachments[0]).toMatchObject({ jobId: "job-1", afterEventId: 0 });
  });

  it("adopts the job left in session storage by a reload, replaying it from event 0", async () => {
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-stored", threadId: "thread-a", lastEventId: 9 }));
    const { api, attachments } = fakeStreamApi();

    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);

    await waitFor(() => expect(attachments).toHaveLength(1));
    // The store held no text, so a stored cursor would leave the start of the answer out.
    expect(attachments[0]).toMatchObject({ jobId: "job-stored", afterEventId: 0 });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-stored", threadId: "thread-a" });
  });

  it("resumes a job the store already tracks from the store's cursor, not from storage", async () => {
    useChatStore.getState().beginGeneration("job-warm", "thread-a");
    useChatStore.getState().appendContentToken("job-warm", "already here");
    useChatStore.getState().setGenerationCursor("job-warm", 12);
    const { api, attachments } = fakeStreamApi();

    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);

    await waitFor(() => expect(attachments).toHaveLength(1));
    expect(attachments[0]).toMatchObject({ jobId: "job-warm", afterEventId: 12 });
    expect(useChatStore.getState().generation.partialContent).toBe("already here");
  });

  it("adopts a stored job only on its first look, never after the store has gone idle", async () => {
    const { api, attachments } = fakeStreamApi();
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));
    await act(async () => {
      attachments[0].emit({ event_id: 1, event: "generation.completed" });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());

    // A copy that could not be cleared (storage denied it) must not bring the
    // finished job back the next time the store is idle: put one there, then
    // let the store go idle again by way of a second job.
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-1", threadId: "thread-a", lastEventId: 1 }));
    act(() => trackGeneration("job-2", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(2));
    await act(async () => {
      attachments[1].emit({ event_id: 1, event: "generation.completed" });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());
    await settleTimers();

    expect(attachments.map((attachment) => attachment.jobId)).toEqual(["job-1", "job-2"]);
  });

  it("does not attach twice to the same job", async () => {
    const { api, attachments } = fakeStreamApi();
    const { rerender } = render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));

    rerender(<GenerationStreamHost api={api} onSessionExpired={() => undefined} />);
    await settleTimers();

    expect(attachments).toHaveLength(1);
  });

  it("reloads the chat and updates the sidebar before ending the generation, with no page mounted", async () => {
    const { api, attachments } = fakeStreamApi();
    useChatStore.getState().setChats([{ id: "thread-a", title: "New Chat", timestamp: "2026-01-01T00:00:00Z" }]);
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));

    await act(async () => {
      attachments[0].emit({ event_id: 1, event: "generation.completed" });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());

    expect(api.chat).toHaveBeenCalledWith("thread-a");
    expect(useChatStore.getState().chats[0]).toMatchObject({ id: "thread-a", title: "Saved title" });
    expect(useChatStore.getState().lastCompletion).toMatchObject({
      threadId: "thread-a",
      chat: { id: "thread-a", messages: [{ content: "Saved answer" }] },
      clearRequested: false,
    });
    expect(readActiveJob()).toBeNull();
  });

  it("publishes a completion without a chat when the reload fails, and still ends the generation", async () => {
    const { api, attachments } = fakeStreamApi({
      chat: vi.fn().mockRejectedValue(new ApiError(503, "Unavailable.")) as unknown as CortexApi["chat"],
    });
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));

    await act(async () => {
      attachments[0].emit({ event_id: 1, event: "generation.completed" });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());

    expect(useChatStore.getState().lastCompletion).toMatchObject({ threadId: "thread-a", chat: null });
  });

  it("carries a model's clear proposal on the completion", async () => {
    const { api, attachments } = fakeStreamApi();
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-clear", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));

    await act(async () => {
      attachments[0].emit({ event_id: 1, event: "generation.completed", data: { clear_requested: true } });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());

    expect(useChatStore.getState().lastCompletion).toMatchObject({ jobId: "job-clear", clearRequested: true });
  });

  it("publishes a failure for the chat view, and still reconciles the chat", async () => {
    const { api, attachments } = fakeStreamApi();
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));

    await act(async () => {
      attachments[0].emit({ event_id: 1, event: "generation.failed", data: { message: "The model stopped." } });
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());

    expect(useChatStore.getState().lastFailure).toMatchObject({ threadId: "thread-a", message: "The model stopped." });
    expect(useChatStore.getState().lastCompletion).toMatchObject({ threadId: "thread-a" });
  });

  it("stops following the stream when the workspace goes away, and keeps the job tracked for the next one", async () => {
    const { api, attachments } = fakeStreamApi();
    const { unmount } = render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(attachments).toHaveLength(1));
    expect(attachments[0].signal?.aborted).toBe(false);

    unmount();

    expect(attachments[0].signal?.aborted).toBe(true);
    expect(useChatStore.getState().generation.jobId).toBe("job-1");
    expect(readActiveJob()).toMatchObject({ jobId: "job-1" });
    expect(useChatStore.getState().lastFailure).toBeNull();
  });
});
