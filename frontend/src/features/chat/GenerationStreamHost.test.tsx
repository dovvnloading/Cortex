import { act, render, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
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

  it("does not attach again to a job whose consumer stopped on a refused session", async () => {
    // The app leaves the workspace mounted when it finds that a newer session
    // exists, so the host outlives the refusal with the job still tracked, and
    // its effect re-runs for any change of dependency. Attaching again there
    // would run a second consumer behind the very refusal the app is acting on.
    const streamGeneration = vi.fn((_jobId: string, _onEvent: unknown, options: { signal?: AbortSignal } = {}) => {
      if (streamGeneration.mock.calls.length === 1) return Promise.reject(new ApiError(401, "Local session expired."));
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const { api } = fakeStreamApi({ streamGeneration: streamGeneration as unknown as CortexApi["streamGeneration"] });
    const onSessionExpired = vi.fn();
    const { rerender, unmount } = render(<GenerationStreamHost api={api} onSessionExpired={onSessionExpired} />);
    act(() => trackGeneration("job-1", "thread-a"));
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(streamGeneration).toHaveBeenCalledTimes(1);
    expect(useChatStore.getState().generation.jobId).toBe("job-1");

    rerender(<GenerationStreamHost api={api} onSessionExpired={() => undefined} />);
    await settleTimers();

    expect(streamGeneration).toHaveBeenCalledTimes(1);

    // The claim belongs to this host: the workspace that replaces it once the
    // session is renewed picks the job up again, from where the store left it.
    unmount();
    render(<GenerationStreamHost api={api} onSessionExpired={onSessionExpired} />);
    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2));
    expect(streamGeneration.mock.calls[1][2]).toMatchObject({ afterEventId: 0 });
  });

  it("keeps following a job when the stream is cut by an abort that was not the host's own", async () => {
    // The host will not attach to a job it already attached to, so a consumer
    // that gave up on such an abort would leave the job tracked with nothing
    // reading it. The consumer must check the job and open the stream again.
    const streamGeneration = vi.fn((_jobId: string, _onEvent: unknown, options: { signal?: AbortSignal } = {}) => {
      if (streamGeneration.mock.calls.length === 1) return Promise.reject(new ApiError(0, "The request was cancelled.", "aborted"));
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const generationStatus = vi.fn().mockResolvedValue({ job_id: "job-1", kind: "generation", status: "running", sequence: 1 });
    const { api } = fakeStreamApi({
      streamGeneration: streamGeneration as unknown as CortexApi["streamGeneration"],
      generationStatus,
    });
    render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);

    act(() => trackGeneration("job-1", "thread-a"));

    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect((streamGeneration.mock.calls[1][2] as { signal: AbortSignal }).signal.aborted).toBe(false);
    expect(useChatStore.getState().generation.jobId).toBe("job-1");
  });

  it("does not adopt a stored job when its effect re-runs with the store idle after a first look found nothing", async () => {
    const { api, attachments } = fakeStreamApi();
    const { rerender } = render(<GenerationStreamHost api={api} onSessionExpired={expireSession} />);
    // The first look happens a tick after mounting. A zero-delay timer queued
    // now runs after it, so nothing is left to find.
    await settleTimers();
    expect(attachments).toHaveLength(0);

    // Storage now holds a job (a copy that a failed clear left behind). Only
    // the first look may adopt one; a later run of the effect must not.
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-left-over", threadId: "thread-a", lastEventId: 3 }));
    rerender(<GenerationStreamHost api={api} onSessionExpired={() => undefined} />);
    await settleTimers();

    expect(attachments).toHaveLength(0);
    expect(useChatStore.getState().generation.jobId).toBeNull();
  });

  describe("under React.StrictMode", () => {
    // StrictMode mounts, unmounts and mounts again on the same instance, in
    // development, to expose effects that are not safe to repeat. What it
    // must not leave behind is a second consumer for the job -- or, worse, none
    // that is alive: an attach made before the simulated unmount is aborted by
    // it, and a guard that remembers the attach then refuses to make another.
    const strict = (api: CortexApi) => (
      <StrictMode>
        <GenerationStreamHost api={api} onSessionExpired={expireSession} />
      </StrictMode>
    );

    it("attaches one live consumer to a job left in session storage", async () => {
      window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-stored", threadId: "thread-a", lastEventId: 9 }));
      const { api, attachments } = fakeStreamApi();

      render(strict(api));

      await waitFor(() => expect(attachments).toHaveLength(1));
      await settleTimers();
      expect(attachments).toHaveLength(1);
      expect(attachments[0]).toMatchObject({ jobId: "job-stored", afterEventId: 0 });
      expect(attachments[0].signal?.aborted).toBe(false);
    });

    it("attaches one live consumer to a job the store already tracks", async () => {
      useChatStore.getState().beginGeneration("job-warm", "thread-a");
      useChatStore.getState().setGenerationCursor("job-warm", 12);
      const { api, attachments } = fakeStreamApi();

      render(strict(api));

      await waitFor(() => expect(attachments).toHaveLength(1));
      await settleTimers();
      expect(attachments).toHaveLength(1);
      expect(attachments[0]).toMatchObject({ jobId: "job-warm", afterEventId: 12 });
      expect(attachments[0].signal?.aborted).toBe(false);
    });

    it("attaches one live consumer to a job a page starts after mounting", async () => {
      const { api, attachments } = fakeStreamApi();
      render(strict(api));
      await settleTimers();

      act(() => trackGeneration("job-1", "thread-a"));

      expect(attachments).toHaveLength(1);
      await settleTimers();
      expect(attachments).toHaveLength(1);
      expect(attachments[0].signal?.aborted).toBe(false);
    });

    it("keeps one live consumer at a time across a real unmount and a new mount", async () => {
      const { api, attachments } = fakeStreamApi();
      const first = render(strict(api));
      act(() => trackGeneration("job-1", "thread-a"));
      await waitFor(() => expect(attachments).toHaveLength(1));

      first.unmount();
      expect(attachments[0].signal?.aborted).toBe(true);
      render(strict(api));
      await waitFor(() => expect(attachments).toHaveLength(2));
      await settleTimers();

      expect(attachments).toHaveLength(2);
      expect(attachments.map((attachment) => attachment.signal?.aborted)).toEqual([true, false]);
    });
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
