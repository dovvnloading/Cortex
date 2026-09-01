import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { useUiStore } from "../stores/useUiStore";
import { readActiveJob, reconnectDelay, useGenerationStream } from "./useGenerationStream";

type FakeGenerationEvent = { event: string; [key: string]: unknown };

/** Mimics the real streamGeneration: the SSE connection closes (promise resolves) right after a terminal event. */
function terminalAwareStream() {
  let emit: ((event: FakeGenerationEvent) => void) | null = null;
  const streamGeneration = vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) =>
    new Promise<void>((resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      emit = (event) => {
        (onEvent as (event: unknown) => void)(event);
        if (["generation.completed", "generation.failed", "generation.cancelled"].includes(event.event)) resolve();
      };
    }),
  );
  return { streamGeneration, emitEvent: (event: FakeGenerationEvent) => emit?.(event) };
}

function fakeApi(overrides: Partial<CortexApi> = {}): CortexApi {
  return {
    streamGeneration: vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => new Promise<void>((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })),
    generationStatus: vi.fn(),
    ...overrides,
  } as unknown as CortexApi;
}

const ignoreSessionExpiry = () => undefined;

describe("useGenerationStream", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    useUiStore.setState({ toasts: [] });
  });

  it("start() persists the job to sessionStorage and moves the store to starting", () => {
    const api = fakeApi();
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-1", "thread-1", onCompleted, onFailed);
    });

    expect(readActiveJob()).toEqual({ jobId: "job-1", threadId: "thread-1", lastEventId: 0 });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-1", threadId: "thread-1" });
  });

  it("ignores a valid JSON value that is not a complete persisted generation", () => {
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-only" }));

    expect(readActiveJob()).toBeNull();
  });

  it("batches rapid content_delta events into the store via requestAnimationFrame", async () => {
    let emit: ((event: unknown) => void) | null = null;
    const api = fakeApi({
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }),
    });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-2", "thread-2", onCompleted, onFailed);
    });
    await waitFor(() => expect(emit).not.toBeNull());

    act(() => {
      emit!({ event_id: 1, event: "generation.content_delta", job_id: "job-2", thread_id: "thread-2", data: { delta: "Hel" } });
      emit!({ event_id: 2, event: "generation.content_delta", job_id: "job-2", thread_id: "thread-2", data: { delta: "lo" } });
    });

    await waitFor(() => expect(useChatStore.getState().generation.partialContent).toBe("Hello"));
    expect(useChatStore.getState().generation.phase).toBe("streaming");
  });

  it("generation.persisting flushes buffered text and marks the answer ready while the job is still running", async () => {
    let emit: ((event: unknown) => void) | null = null;
    const api = fakeApi({
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }),
    });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-persist", "thread-persist", vi.fn().mockResolvedValue(undefined), vi.fn());
    });
    await waitFor(() => expect(emit).not.toBeNull());

    act(() => {
      emit!({ event_id: 1, event: "generation.content_delta", job_id: "job-persist", thread_id: "thread-persist", data: { delta: "The answer" } });
      emit!({ event_id: 2, event: "generation.persisting", job_id: "job-persist", thread_id: "thread-persist", data: { message: "Saving the response." } });
    });

    // Bookkeeping (title generation) may still be running -- the job must
    // stay active -- but the answer text is final and known immediately,
    // without waiting an animation frame for the flush.
    expect(useChatStore.getState().generation.partialContent).toBe("The answer");
    expect(useChatStore.getState().generation.contentReady).toBe(true);
    expect(useChatStore.getState().generation.jobId).toBe("job-persist");
  });

  it("ignores an event whose thread_id does not match the tracked job", async () => {
    let emit: ((event: unknown) => void) | null = null;
    const api = fakeApi({
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }),
    });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-3", "thread-3", vi.fn().mockResolvedValue(undefined), vi.fn());
    });
    await waitFor(() => expect(emit).not.toBeNull());

    act(() => {
      emit!({ event_id: 1, event: "generation.content_delta", job_id: "job-3", thread_id: "some-other-thread", data: { delta: "wrong" } });
    });

    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(useChatStore.getState().generation.partialContent).toBe("");
  });

  it("on generation.completed calls onCompleted, clears sessionStorage, and resets the store to idle", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-4", "thread-4", onCompleted, vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({ event_id: 1, event: "generation.content_delta", job_id: "job-4", thread_id: "thread-4", data: { delta: "done" } });
      emitEvent({ event_id: 2, event: "generation.completed", job_id: "job-4", thread_id: "thread-4", data: {} });
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-4"));
    // The last buffered token flushes even though completion fired in the same tick.
    await waitFor(() => expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" }));
    expect(readActiveJob()).toBeNull();
  });

  it("forwards a model clear proposal only after a successful terminal event", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-clear", "thread-clear", onCompleted, vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.completed",
        job_id: "job-clear",
        thread_id: "thread-clear",
        data: { clear_requested: true },
      });
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-clear", true, "job-clear"));
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it("forwards a clear proposal when reconnect status confirms successful completion", async () => {
    const generationStatus = vi.fn().mockResolvedValue({
      job_id: "job-clear-status",
      kind: "generation",
      status: "succeeded",
      sequence: 2,
      result: { clear_requested: true },
    });
    const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
    const api = fakeApi({ streamGeneration, generationStatus });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-clear-status", "thread-clear-status", onCompleted, vi.fn());
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-clear-status", true, "job-clear-status"));
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it("surfaces a refused code proposal, which is otherwise invisible", async () => {
    // The rejected block is stripped from the answer so it cannot teach the
    // model its own malformed format on the next turn. That leaves the toast
    // as the only place the user learns the task they asked for never ran.
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-rej", "thread-rej", vi.fn().mockResolvedValue(undefined), vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.status",
        job_id: "job-rej",
        thread_id: "thread-rej",
        data: {
          message: "Cortex only runs code without imports.",
          code_execution_rejection: {
            code: "imports_not_allowed",
            message: "Cortex only runs code without imports.",
          },
        },
      });
    });

    await waitFor(() =>
      expect(useUiStore.getState().toasts.map((toast) => toast.message)).toContain(
        "Cortex only runs code without imports.",
      ),
    );
  });

  it("ignores a malformed rejection payload rather than showing an empty toast", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-rej2", "thread-rej2", vi.fn().mockResolvedValue(undefined), vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.status",
        job_id: "job-rej2",
        thread_id: "thread-rej2",
        data: { code_execution_rejection: { code: "imports_not_allowed" } },
      });
    });

    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(useUiStore.getState().toasts).toHaveLength(0);
  });

  it("waits for onCompleted's reload to finish before clearing the store's jobId", async () => {
    // Regression test: the store's jobId gates whether ChatPage's pending
    // bubble is mounted. onCompleted (reconcileChat) reloads the chat so the
    // real, persisted message can take that bubble's place. If the store
    // cleared jobId before that reload resolved, the pending bubble would
    // unmount with nothing yet loaded to replace it -- the response visibly
    // vanishes for the length of that request, then "pops" back in once it
    // resolves.
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    let resolveReload: (() => void) | null = null;
    const onCompleted = vi.fn(() => new Promise<void>((resolve) => { resolveReload = resolve; }));

    act(() => {
      result.current.start("job-11", "thread-11", onCompleted, vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({ event_id: 1, event: "generation.content_delta", job_id: "job-11", thread_id: "thread-11", data: { delta: "done" } });
      emitEvent({ event_id: 2, event: "generation.completed", job_id: "job-11", thread_id: "thread-11", data: {} });
    });
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-11"));

    // The reload is deliberately left pending -- the job must still read as
    // active so the pending bubble stays mounted.
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(useChatStore.getState().generation.jobId).toBe("job-11");

    act(() => resolveReload?.());

    await waitFor(() => expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" }));
  });

  it("on generation.failed calls onFailed with the event message and resets the store", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-5", "thread-5", vi.fn().mockResolvedValue(undefined), onFailed);
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({ event_id: 1, event: "generation.failed", job_id: "job-5", thread_id: "thread-5", data: { message: "Model crashed." } });
    });

    await waitFor(() => expect(onFailed).toHaveBeenCalledWith("thread-5", "Model crashed."));
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());
  });

  it("dedupes a second consume() call for the same jobId already in flight", async () => {
    const streamGeneration = vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => new Promise<void>((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }));
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const job = { jobId: "job-6", threadId: "thread-6", lastEventId: 0 };

    await act(async () => {
      void result.current.consume(job, vi.fn().mockResolvedValue(undefined), vi.fn());
      void result.current.consume(job, vi.fn().mockResolvedValue(undefined), vi.fn());
      await Promise.resolve();
    });

    expect(streamGeneration).toHaveBeenCalledTimes(1);
  });

  it("stop() aborts the in-flight stream without treating it as a terminal failure", async () => {
    const api = fakeApi();
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-7", "thread-7", vi.fn().mockResolvedValue(undefined), onFailed);
    });
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBe("job-7"));

    act(() => {
      result.current.stop();
    });
    await new Promise((resolve) => window.setTimeout(resolve, 20));

    expect(onFailed).not.toHaveBeenCalled();
  });

  it("clears the tracked generation and expires the session without reconnecting when the stream rejects with a 401", async () => {
    const streamGeneration = vi.fn().mockRejectedValue(new ApiError(401, "Local session expired."));
    const api = fakeApi({ streamGeneration });
    const onSessionExpired = vi.fn();
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, onSessionExpired));

    act(() => {
      result.current.start("job-8", "thread-8", vi.fn().mockResolvedValue(undefined), onFailed);
    });

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(streamGeneration).toHaveBeenCalledTimes(1);
    expect(api.generationStatus).not.toHaveBeenCalled();
    expect(onFailed).not.toHaveBeenCalled();
    expect(readActiveJob()).toBeNull();
    expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" });
  });

  it("clears the tracked generation and expires the session when the status fallback rejects with a 401", async () => {
    const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
    const generationStatus = vi.fn().mockRejectedValue(new ApiError(401, "Local session expired."));
    const api = fakeApi({ streamGeneration, generationStatus });
    const onSessionExpired = vi.fn();
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, onSessionExpired));

    act(() => {
      result.current.start("job-status-401", "thread-status-401", vi.fn().mockResolvedValue(undefined), onFailed);
    });

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(streamGeneration).toHaveBeenCalledTimes(1);
    expect(generationStatus).toHaveBeenCalledTimes(1);
    expect(onFailed).not.toHaveBeenCalled();
    expect(readActiveJob()).toBeNull();
    expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" });
  });

  it("stops retrying and clears the tracked generation when the status fallback says the job is gone", async () => {
    const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
    const generationStatus = vi.fn().mockRejectedValue(new ApiError(404, "Generation job not found."));
    const api = fakeApi({ streamGeneration, generationStatus });
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-status-404", "thread-status-404", vi.fn().mockResolvedValue(undefined), onFailed);
    });

    await waitFor(() => expect(onFailed).toHaveBeenCalledWith("thread-status-404", "Generation job not found."));
    expect(streamGeneration).toHaveBeenCalledTimes(1);
    expect(generationStatus).toHaveBeenCalledTimes(1);
    expect(readActiveJob()).toBeNull();
    expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" });
  });

  it("reconnects with the accumulated cursor after a transient (non-401) stream error", async () => {
    const generationStatus = vi.fn().mockResolvedValue({ job_id: "job-9", kind: "generation", status: "running", sequence: 1 });
    const streamGeneration = vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
      if (streamGeneration.mock.calls.length === 1) {
        (onEvent as (event: unknown) => void)({ event_id: 5, event: "generation.content_delta", job_id: "job-9", thread_id: "thread-9", data: { delta: "x" } });
        return Promise.reject(new Error("connection dropped"));
      }
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const api = fakeApi({ streamGeneration, generationStatus });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      void result.current.consume({ jobId: "job-9", threadId: "thread-9", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
    });

    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(streamGeneration.mock.calls[1][2]).toMatchObject({ afterEventId: 5 });

    act(() => result.current.stop());
  });

  it("uses jittered exponential reconnect delays with a hard ceiling", () => {
    expect(reconnectDelay(0, () => 0)).toBe(200);
    expect(reconnectDelay(0, () => 0.5)).toBe(250);
    expect(reconnectDelay(1, () => 1)).toBe(600);
    expect(reconnectDelay(100, () => 1)).toBe(30_000);
  });

  it("pauses reconnects while offline and resumes when the browser comes online", async () => {
    const hadOwnOnlineProperty = Object.prototype.hasOwnProperty.call(window.navigator, "onLine");
    const onlineDescriptor = Object.getOwnPropertyDescriptor(window.navigator, "onLine");
    Object.defineProperty(window.navigator, "onLine", { configurable: true, value: false });
    try {
      const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
      const generationStatus = vi.fn().mockResolvedValue({ job_id: "job-offline", kind: "generation", status: "running", sequence: 1 });
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
      useChatStore.getState().beginGeneration("job-offline", "thread-offline");

      act(() => {
        void result.current.consume({ jobId: "job-offline", threadId: "thread-offline", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
      });
      await waitFor(() => expect(useChatStore.getState().generation.statusText).toContain("paused while offline"));
      expect(streamGeneration).toHaveBeenCalledTimes(1);

      Object.defineProperty(window.navigator, "onLine", { configurable: true, value: true });
      act(() => window.dispatchEvent(new Event("online")));
      await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2));
      act(() => result.current.stop());
    } finally {
      if (onlineDescriptor) Object.defineProperty(window.navigator, "onLine", onlineDescriptor);
      else if (!hadOwnOnlineProperty) delete (window.navigator as { onLine?: boolean }).onLine;
    }
  });

  it("cancels a pending reconnect timer when stopped", async () => {
    vi.useFakeTimers();
    try {
      const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
      const generationStatus = vi.fn().mockResolvedValue({ job_id: "job-cleanup", kind: "generation", status: "running", sequence: 1 });
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

      act(() => {
        void result.current.consume({ jobId: "job-cleanup", threadId: "thread-cleanup", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
      });
      await act(async () => {
        for (let index = 0; index < 4; index += 1) await Promise.resolve();
      });
      expect(streamGeneration).toHaveBeenCalledTimes(1);
      expect(generationStatus).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(1);

      act(() => result.current.stop());
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps retrying, and does not orphan the job, when the status-check fallback also fails", async () => {
    // Regression test: the stream connection drops AND the fallback status
    // check used to confirm what happened also fails (e.g. the backend is
    // briefly overloaded). The old code let that second failure escape
    // uncaught, which skipped past the loop entirely without ever setting
    // `terminal` -- onFailed() still fired once, but nothing was left
    // running to ever call endGeneration(), so the composer and pending
    // message bubble reported "Generating" forever with no way to recover.
    useChatStore.getState().beginGeneration("job-10", "thread-10");
    const generationStatus = vi.fn().mockRejectedValue(new Error("status endpoint unreachable"));
    const streamGeneration = vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => {
      if (streamGeneration.mock.calls.length === 1) {
        return Promise.reject(new Error("connection dropped"));
      }
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const api = fakeApi({ streamGeneration, generationStatus });
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      void result.current.consume({ jobId: "job-10", threadId: "thread-10", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), onFailed);
    });

    await waitFor(() => expect(generationStatus).toHaveBeenCalled());
    // The loop must still be alive and retrying -- not orphaned.
    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(onFailed).not.toHaveBeenCalled();
    expect(useChatStore.getState().generation.jobId).toBe("job-10");

    act(() => result.current.stop());
  });

  it("keeps streaming when session storage is unavailable", async () => {
    // Regression test: a browser that denies storage access (or has hit its
    // quota) throws from setItem. persistActiveJob() runs inside the
    // synchronous SSE event handler, so an escaping throw reached consume()
    // as a stream failure -- it reconnected from the same cursor, replayed
    // the same event, threw again, and looped forever without ever
    // rendering a token.
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("QuotaExceededError", "QuotaExceededError");
    });
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("SecurityError", "SecurityError");
    });
    try {
      expect(readActiveJob()).toBeNull();

      const { streamGeneration, emitEvent } = terminalAwareStream();
      const generationStatus = vi.fn();
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

      act(() => {
        result.current.start("job-storage", "thread-storage", vi.fn().mockResolvedValue(undefined), vi.fn());
      });
      await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(1));

      act(() => {
        emitEvent({ event_id: 1, event: "generation.content_delta", job_id: "job-storage", thread_id: "thread-storage", data: { delta: "Hello" } });
      });

      await waitFor(() => expect(useChatStore.getState().generation.partialContent).toBe("Hello"));
      // No reconnect: the storage failure must not be mistaken for a dropped
      // connection.
      expect(streamGeneration).toHaveBeenCalledTimes(1);
      expect(generationStatus).not.toHaveBeenCalled();

      act(() => result.current.stop());
    } finally {
      setItem.mockRestore();
      getItem.mockRestore();
    }
  });

  it("does not release another job's consumer claim when a previous job finishes late", async () => {
    // Regression test: a completed job awaits its transcript reload before
    // unwinding. If the user starts a second generation during that window,
    // the first job's cleanup used to clear consumingRef unconditionally --
    // dropping the second job's claim and letting a duplicate consumer
    // attach to it (double-counting every token it then receives).
    const streams = new Map<string, (event: FakeGenerationEvent) => void>();
    const streamGeneration = vi.fn((jobId: string, onEvent, options: { signal?: AbortSignal } = {}) =>
      new Promise<void>((resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        streams.set(jobId, (event) => {
          (onEvent as (event: unknown) => void)(event);
          if (event.event === "generation.completed") resolve();
        });
      }),
    );
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    let finishReload: (() => void) | null = null;
    const slowReload = vi.fn(() => new Promise<void>((resolve) => { finishReload = resolve; }));

    act(() => {
      void result.current.consume({ jobId: "job-a", threadId: "thread-a", lastEventId: 0 }, slowReload, vi.fn());
    });
    await waitFor(() => expect(streams.has("job-a")).toBe(true));

    act(() => {
      streams.get("job-a")!({ event_id: 1, event: "generation.completed", job_id: "job-a", thread_id: "thread-a", data: {} });
    });
    // job-a is now parked in its finally block, awaiting the reload.
    await waitFor(() => expect(slowReload).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.start("job-b", "thread-b", vi.fn().mockResolvedValue(undefined), vi.fn());
    });
    await waitFor(() => expect(streams.has("job-b")).toBe(true));
    expect(streamGeneration).toHaveBeenCalledTimes(2);

    await act(async () => {
      finishReload?.();
      await Promise.resolve();
    });

    // job-b is still the active consumer, so a second consume() for it is a
    // no-op rather than a duplicate connection.
    await act(async () => {
      void result.current.consume({ jobId: "job-b", threadId: "thread-b", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
      await Promise.resolve();
    });
    expect(streamGeneration).toHaveBeenCalledTimes(2);

    act(() => result.current.stop());
  });
});
