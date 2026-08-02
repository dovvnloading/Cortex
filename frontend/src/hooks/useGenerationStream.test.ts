import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { readActiveJob, useGenerationStream } from "./useGenerationStream";

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

describe("useGenerationStream", () => {
  afterEach(() => window.sessionStorage.clear());

  it("start() persists the job to sessionStorage and moves the store to starting", () => {
    const api = fakeApi();
    const { result } = renderHook(() => useGenerationStream(api));
    const onCompleted = vi.fn().mockResolvedValue(undefined);
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-1", "thread-1", onCompleted, onFailed);
    });

    expect(readActiveJob()).toEqual({ jobId: "job-1", threadId: "thread-1", lastEventId: 0 });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-1", threadId: "thread-1" });
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
    const { result } = renderHook(() => useGenerationStream(api));
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
    const { result } = renderHook(() => useGenerationStream(api));

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
    const { result } = renderHook(() => useGenerationStream(api));
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

  it("on generation.failed calls onFailed with the event message and resets the store", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api));
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
    const { result } = renderHook(() => useGenerationStream(api));
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
    const { result } = renderHook(() => useGenerationStream(api));
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

  it("stops without reconnecting when the stream rejects with a 401", async () => {
    const streamGeneration = vi.fn().mockRejectedValue(new ApiError(401, "Local session expired."));
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api));

    await act(async () => {
      await result.current.consume({ jobId: "job-8", threadId: "thread-8", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
    });

    expect(streamGeneration).toHaveBeenCalledTimes(1);
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
    const { result } = renderHook(() => useGenerationStream(api));

    act(() => {
      void result.current.consume({ jobId: "job-9", threadId: "thread-9", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
    });

    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(streamGeneration.mock.calls[1][2]).toMatchObject({ afterEventId: 5 });

    act(() => result.current.stop());
  });
});
