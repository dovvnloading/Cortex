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

/**
 * Let every promise continuation that is already queued run to completion.
 *
 * A timer callback only runs once the microtask queue is empty, so a zero-delay
 * timer is a barrier that holds however slow the machine is. That is what a
 * "nothing further happened" assertion needs when the code under test only
 * ever continues through promises. It is not a wall-clock wait: never use it
 * to give a timer- or frame-driven effect time to show up.
 */
const settlePromises = () =>
  act(async () => {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
  });

describe("useGenerationStream", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    useUiStore.setState({ toasts: [] });
    // A generation kept across a 401 is deliberately left tracked; do not let
    // it leak into the next test.
    const { generation, endGeneration } = useChatStore.getState();
    if (generation.jobId) endGeneration(generation.jobId);
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

    // The stream delivers events in order, so the foreign event has been
    // handled by the time the matching one is. Waiting for that matching
    // event's text to appear is the positive signal; the foreign text must
    // then be absent from what was flushed with it. A wall-clock sleep before
    // an "is still empty" check passes vacuously when the frame is late.
    act(() => {
      emit!({ event_id: 1, event: "generation.content_delta", job_id: "job-3", thread_id: "some-other-thread", data: { delta: "wrong" } });
      emit!({ event_id: 1, event: "generation.content_delta", job_id: "job-3", thread_id: "thread-3", data: { delta: "right" } });
    });

    await waitFor(() => expect(useChatStore.getState().generation.partialContent).not.toBe(""));
    expect(useChatStore.getState().generation.partialContent).toBe("right");
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

  it("records an answer left untranslated when the completion carries a translation error", async () => {
    useChatStore.setState({ untranslatedMessageIds: {} });
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-untranslated", "thread-untranslated", onCompleted, vi.fn());
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.completed",
        job_id: "job-untranslated",
        thread_id: "thread-untranslated",
        data: { assistant_message_id: "assistant-7", translation_error: "Translation failed. Please try again." },
      });
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalled());
    expect(useChatStore.getState().untranslatedMessageIds).toEqual({ "assistant-7": true });
  });

  it("records an untranslated answer from the status fallback, and ignores a translated one", async () => {
    useChatStore.setState({ untranslatedMessageIds: {} });
    const generationStatus = vi.fn().mockResolvedValue({
      job_id: "job-untranslated-status",
      kind: "generation",
      status: "succeeded",
      sequence: 2,
      result: { assistant_message_id: "assistant-8", translation_error: "Translation returned an empty result." },
    });
    const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
    const api = fakeApi({ streamGeneration, generationStatus });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-untranslated-status", "thread-untranslated-status", onCompleted, vi.fn());
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalled());
    expect(useChatStore.getState().untranslatedMessageIds).toEqual({ "assistant-8": true });

    const { streamGeneration: cleanStream, emitEvent } = terminalAwareStream();
    const cleanApi = fakeApi({ streamGeneration: cleanStream });
    const clean = renderHook(() => useGenerationStream(cleanApi, ignoreSessionExpiry));
    const cleanCompleted = vi.fn().mockResolvedValue(undefined);
    act(() => {
      clean.result.current.start("job-translated", "thread-translated", cleanCompleted, vi.fn());
    });
    await waitFor(() => expect(cleanStream).toHaveBeenCalled());
    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.completed",
        job_id: "job-translated",
        thread_id: "thread-translated",
        data: { assistant_message_id: "assistant-9", translation_error: null },
      });
    });
    await waitFor(() => expect(cleanCompleted).toHaveBeenCalled());
    expect(useChatStore.getState().untranslatedMessageIds).toEqual({ "assistant-8": true });
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
      // A well-formed rejection right behind it. Events are handled in order,
      // so once its toast is up the malformed one has been handled too, and
      // it must not have produced a toast of its own. Announcing the valid one
      // also shows the malformed one did not use up the once-only notice.
      emitEvent({
        event_id: 2,
        event: "generation.status",
        job_id: "job-rej2",
        thread_id: "thread-rej2",
        data: {
          code_execution_rejection: { code: "imports_not_allowed", message: "Cortex only runs code without imports." },
        },
      });
    });

    await waitFor(() => expect(useUiStore.getState().toasts).not.toHaveLength(0));
    expect(useUiStore.getState().toasts.map((toast) => toast.message)).toEqual([
      "Cortex only runs code without imports.",
    ]);
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
    // active so the pending bubble stays mounted. Everything between the
    // terminal event and the store reset runs through promises, so once they
    // have all settled the only thing left holding the reset back is the
    // reload itself.
    await settlePromises();
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

  it("a stop that kept the answer reloads the chat without reporting a failure", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onCompleted = vi.fn().mockResolvedValue(undefined);
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-kept", "thread-kept", onCompleted, onFailed);
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({
        event_id: 1,
        event: "generation.cancelled",
        job_id: "job-kept",
        thread_id: "thread-kept",
        data: { message: "Job cancelled.", assistant_message_id: "assistant-kept" },
      });
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-kept"));
    expect(onFailed).not.toHaveBeenCalled();
  });

  it("a stop with nothing to keep still reports it, so Retry stays reachable", async () => {
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const onFailed = vi.fn();

    act(() => {
      result.current.start("job-empty", "thread-empty", vi.fn().mockResolvedValue(undefined), onFailed);
    });
    await waitFor(() => expect(streamGeneration).toHaveBeenCalled());

    act(() => {
      emitEvent({ event_id: 1, event: "generation.cancelled", job_id: "job-empty", thread_id: "thread-empty", data: { message: "Job cancelled." } });
    });

    await waitFor(() => expect(onFailed).toHaveBeenCalledWith("thread-empty", "Job cancelled."));
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

    // The positive signal: the abort reached the connection the hook opened.
    // The rejection it causes is then handled entirely through promises, so
    // once those have settled a wrongly reported failure would have shown.
    const { signal } = (api.streamGeneration as ReturnType<typeof vi.fn>).mock.calls[0][2] as { signal: AbortSignal };
    expect(signal.aborted).toBe(false);

    act(() => {
      result.current.stop();
    });
    expect(signal.aborted).toBe(true);
    await settlePromises();

    expect(onFailed).not.toHaveBeenCalled();
  });

  it("keeps the tracked generation and persisted job when the stream rejects with 401", async () => {
    // The backend owns a generation by installation, not by session, so an
    // expired session says nothing about the job. It must stay tracked for the
    // resume effect to re-attach once the session is renewed.
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
    expect(readActiveJob()).toEqual({ jobId: "job-8", threadId: "thread-8", lastEventId: 0 });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-8", threadId: "thread-8" });
    expect(useChatStore.getState().generation.phase).not.toBe("idle");
  });

  it("re-attaches from the preserved cursor after a 401 once the session is back", async () => {
    const streamGeneration = vi.fn((_jobId, onEvent, options: { signal?: AbortSignal; afterEventId?: number } = {}) => {
      if (streamGeneration.mock.calls.length === 1) {
        (onEvent as (event: unknown) => void)({ event_id: 6, event: "generation.persisting", job_id: "job-401-resume", thread_id: "thread-401-resume", data: {} });
        return Promise.reject(new ApiError(401, "Local session expired."));
      }
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const api = fakeApi({ streamGeneration });
    const onSessionExpired = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, onSessionExpired));
    const onCompleted = vi.fn().mockResolvedValue(undefined);

    act(() => {
      result.current.start("job-401-resume", "thread-401-resume", onCompleted, vi.fn());
    });
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(useChatStore.getState().generationCursor).toBe(6);

    // What ChatPage's resume effect does after the workspace comes back: the
    // claim was released, so the same job can be consumed again.
    const cursor = useChatStore.getState().generationCursor;
    act(() => {
      void result.current.consume({ jobId: "job-401-resume", threadId: "thread-401-resume", lastEventId: cursor }, onCompleted, vi.fn());
    });

    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2));
    expect(streamGeneration.mock.calls[1]?.[2]).toMatchObject({ afterEventId: 6 });
    expect(useChatStore.getState().generation.jobId).toBe("job-401-resume");
    act(() => result.current.stop());
  });

  it("still ends the generation when the job finishes on the re-attached stream", async () => {
    const streamGeneration = vi.fn((_jobId, onEvent) => {
      if (streamGeneration.mock.calls.length === 1) return Promise.reject(new ApiError(401, "Local session expired."));
      (onEvent as (event: unknown) => void)({ event_id: 1, event: "generation.completed", job_id: "job-401-done", thread_id: "thread-401-done", data: {} });
      return Promise.resolve();
    });
    const api = fakeApi({ streamGeneration });
    const onSessionExpired = vi.fn();
    const onCompleted = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useGenerationStream(api, onSessionExpired));

    act(() => {
      result.current.start("job-401-done", "thread-401-done", onCompleted, vi.fn());
    });
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    act(() => {
      void result.current.consume({ jobId: "job-401-done", threadId: "thread-401-done", lastEventId: 0 }, onCompleted, vi.fn());
    });

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-401-done"));
    await waitFor(() => expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" }));
    expect(readActiveJob()).toBeNull();
  });

  it("keeps the tracked generation when the status fallback rejects with a 401", async () => {
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
    expect(readActiveJob()).toEqual({ jobId: "job-status-401", threadId: "thread-status-401", lastEventId: 0 });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-status-401", threadId: "thread-status-401" });
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

  it("treats an unreachable backend as retryable on both the stream and the status check", async () => {
    // The typed network failure is neither a lost session nor a vanished job:
    // the generation is still running one process away, so keep trying.
    const unreachable = () => new ApiError(0, "Cortex could not reach the local backend.", "network");
    const generationStatus = vi.fn().mockRejectedValue(unreachable());
    const streamGeneration = vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => {
      if (streamGeneration.mock.calls.length === 1) return Promise.reject(unreachable());
      return new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const api = fakeApi({ streamGeneration, generationStatus });
    const onSessionExpired = vi.fn();
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, onSessionExpired));

    act(() => {
      result.current.start("job-unreachable", "thread-unreachable", vi.fn().mockResolvedValue(undefined), onFailed);
    });

    await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(generationStatus).toHaveBeenCalledTimes(1);
    expect(onFailed).not.toHaveBeenCalled();
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-unreachable", threadId: "thread-unreachable" });

    act(() => result.current.stop());
  });

  it("gives the status check the stop signal so stopping does not leave it running", async () => {
    const generationStatus = vi.fn((_jobId: string, options: { signal?: AbortSignal } = {}) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new ApiError(0, "The request was cancelled.", "aborted")), { once: true });
    }));
    const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
    const api = fakeApi({ streamGeneration, generationStatus: generationStatus as unknown as CortexApi["generationStatus"] });
    const onFailed = vi.fn();
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

    act(() => {
      result.current.start("job-stop-status", "thread-stop-status", vi.fn().mockResolvedValue(undefined), onFailed);
    });
    await waitFor(() => expect(generationStatus).toHaveBeenCalledTimes(1));
    const { signal } = generationStatus.mock.calls[0][1] as { signal: AbortSignal };
    expect(signal.aborted).toBe(false);

    act(() => result.current.stop());
    expect(signal.aborted).toBe(true);
    await settlePromises();

    // A cancelled check is the caller's own doing: no failure, no more attempts.
    expect(onFailed).not.toHaveBeenCalled();
    expect(streamGeneration).toHaveBeenCalledTimes(1);
    expect(useChatStore.getState().generation.jobId).toBe("job-stop-status");
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

  it("resets the reconnect backoff once a reconnect delivers an event", async () => {
    // Regression test: reconnectAttempt used to only ever increment, so a
    // long generation that reconnects a few times -- each one succeeding --
    // still compounded the same exponential delay as a connection that never
    // recovers. Mocking Math.random to 0.5 makes reconnectDelay's jitter
    // factor exactly 1, so attempt 0 is always 250ms and attempt 1 is always
    // 500ms with no flake risk.
    vi.useFakeTimers();
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0.5);
    try {
      let call = 0;
      const streamGeneration = vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        call += 1;
        if (call === 1) {
          return Promise.reject(new Error("connection dropped"));
        }
        if (call === 2) {
          // Recovers and delivers one event -- this must reset the backoff --
          // before dropping again.
          (onEvent as (event: unknown) => void)({
            event_id: 1,
            event: "generation.content_delta",
            job_id: "job-reset",
            thread_id: "thread-reset",
            data: { delta: "x" },
          });
          return Promise.reject(new Error("connection dropped again"));
        }
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      });
      const generationStatus = vi.fn().mockResolvedValue({ job_id: "job-reset", kind: "generation", status: "running", sequence: 1 });
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));

      act(() => {
        void result.current.consume({ jobId: "job-reset", threadId: "thread-reset", lastEventId: 0 }, vi.fn().mockResolvedValue(undefined), vi.fn());
      });
      await act(async () => {
        for (let index = 0; index < 4; index += 1) await Promise.resolve();
      });
      expect(streamGeneration).toHaveBeenCalledTimes(1);

      // First backoff (attempt 0) is 250ms.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(250);
      });
      await act(async () => {
        for (let index = 0; index < 6; index += 1) await Promise.resolve();
      });
      expect(streamGeneration).toHaveBeenCalledTimes(2);

      // If the event above reset the backoff, the next delay is 250ms again
      // (attempt 0), not 500ms (attempt 1) -- 300ms is enough for the former
      // but not the latter.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300);
      });
      await act(async () => {
        for (let index = 0; index < 6; index += 1) await Promise.resolve();
      });
      expect(streamGeneration).toHaveBeenCalledTimes(3);

      act(() => result.current.stop());
    } finally {
      randomSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("uses jittered exponential reconnect delays with a hard ceiling", () => {
    expect(reconnectDelay(0, () => 0)).toBe(200);
    expect(reconnectDelay(0, () => 0.5)).toBe(250);
    expect(reconnectDelay(1, () => 1)).toBe(600);
    expect(reconnectDelay(100, () => 1)).toBe(30_000);
  });

  it("keeps retrying a dropped stream while the machine is offline", async () => {
    // The backend is loopback-only -- normalizeApiBaseUrl refuses anything else
    // in production -- so navigator.onLine says nothing about reaching it.
    // Gating reconnects on it parked a dropped stream on "Waiting for
    // network..." with no timer and no retry, and a laptop with its adapter
    // off lost the rest of a generation that was still running locally.
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

      // No "online" event is ever dispatched: the machine stays offline
      // throughout, and the retry has to happen anyway.
      await waitFor(() => expect(streamGeneration).toHaveBeenCalledTimes(2));
      expect(useChatStore.getState().generation.statusText).toContain("Retrying in");
      expect(useChatStore.getState().generation.statusText).not.toContain("offline");
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

  it("keeps sessionStorage out of the token hot path but records other frames", async () => {
    // A cold start replays the job from event 0 (see ChatPage's resume
    // effect), so the persisted cursor is never what a resume reads back --
    // only the job and thread identity are. Writing it once per token frame
    // put a JSON.stringify and a synchronous setItem beside the rAF batching
    // that exists to keep that path cheap.
    const { streamGeneration, emitEvent } = terminalAwareStream();
    const api = fakeApi({ streamGeneration });
    const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
    const setItem = vi.spyOn(window.Storage.prototype, "setItem");

    try {
      act(() => {
        result.current.start("job-hot", "thread-hot", vi.fn().mockResolvedValue(undefined), vi.fn());
      });
      await waitFor(() => expect(streamGeneration).toHaveBeenCalled());
      setItem.mockClear();

      act(() => {
        for (let index = 1; index <= 20; index += 1) {
          emitEvent({
            event: "generation.content_delta",
            event_id: index,
            job_id: "job-hot",
            thread_id: "thread-hot",
            data: { delta: "x", message: "Response content available." },
          });
        }
      });
      expect(setItem).not.toHaveBeenCalled();

      act(() => {
        emitEvent({
          event: "generation.persisting",
          event_id: 21,
          job_id: "job-hot",
          thread_id: "thread-hot",
          data: { message: "Saving the response." },
        });
      });
      expect(readActiveJob()).toEqual({ jobId: "job-hot", threadId: "thread-hot", lastEventId: 21 });
    } finally {
      setItem.mockRestore();
      act(() => result.current.stop());
    }
  });

  describe("memories the model suggested", () => {
    afterEach(() => {
      useChatStore.setState({ proposedMemoriesByMessage: {} });
    });

    async function startJob(jobId: string, threadId: string) {
      const { streamGeneration, emitEvent } = terminalAwareStream();
      const api = fakeApi({ streamGeneration });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
      const onCompleted = vi.fn().mockResolvedValue(undefined);
      act(() => {
        result.current.start(jobId, threadId, onCompleted, vi.fn());
      });
      await waitFor(() => expect(streamGeneration).toHaveBeenCalled());
      return { emitEvent, onCompleted };
    }

    it("records the suggestions announced while the answer is being saved, and does not treat them as a clear", async () => {
      const { emitEvent, onCompleted } = await startJob("job-propose", "thread-propose");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.memory_proposed",
          job_id: "job-propose",
          thread_id: "thread-propose",
          data: { message: "Cortex suggested a change.", assistant_message_id: "assistant-1", proposed_memories: ["Likes tea.", "Lives in Oslo."], clear_requested: false },
        });
      });
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-1": ["Likes tea.", "Lives in Oslo."] });
      expect(onCompleted).not.toHaveBeenCalled();

      act(() => {
        emitEvent({
          event_id: 2,
          event: "generation.completed",
          job_id: "job-propose",
          thread_id: "thread-propose",
          data: { assistant_message_id: "assistant-1", proposed_memories: ["Likes tea.", "Lives in Oslo."], clear_requested: false },
        });
      });
      await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("thread-propose"));
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-1": ["Likes tea.", "Lives in Oslo."] });
    });

    it("takes the suggestions from the finished result when no event announced them", async () => {
      const { emitEvent, onCompleted } = await startJob("job-result-only", "thread-result-only");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.completed",
          job_id: "job-result-only",
          thread_id: "thread-result-only",
          data: { assistant_message_id: "assistant-2", proposed_memories: ["Likes tea."] },
        });
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-2": ["Likes tea."] });
    });

    it("forgets an earlier answer's suggestions when a regenerated one has none", async () => {
      useChatStore.getState().setProposedMemories("assistant-3", ["Stale suggestion."]);
      const { emitEvent, onCompleted } = await startJob("job-regenerated", "thread-regenerated");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.completed",
          job_id: "job-regenerated",
          thread_id: "thread-regenerated",
          data: { assistant_message_id: "assistant-3", proposed_memories: [] },
        });
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({});
    });

    it("leaves suggestions alone when the result does not mention them at all", async () => {
      useChatStore.getState().setProposedMemories("assistant-4", ["Kept."]);
      const { emitEvent, onCompleted } = await startJob("job-older-backend", "thread-older-backend");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.completed",
          job_id: "job-older-backend",
          thread_id: "thread-older-backend",
          data: { assistant_message_id: "assistant-4" },
        });
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-4": ["Kept."] });
    });

    it("bounds and cleans what it accepts, since the list is model output", async () => {
      const { emitEvent, onCompleted } = await startJob("job-hostile", "thread-hostile");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.completed",
          job_id: "job-hostile",
          thread_id: "thread-hostile",
          data: {
            assistant_message_id: "assistant-5",
            proposed_memories: [
              "  Likes tea.  ",
              "Likes tea.",
              "   ",
              "x".repeat(501),
              42,
              null,
              { text: "nested" },
              "Fact 1",
              "Fact 2",
              "Fact 3",
              "Fact 4",
              "Fact 5",
            ],
          },
        });
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({
        "assistant-5": ["Likes tea.", "Fact 1", "Fact 2", "Fact 3", "Fact 4"],
      });
    });

    it("ignores a suggestion list with no answer to attach it to, or that is not a list", async () => {
      const { emitEvent, onCompleted } = await startJob("job-malformed", "thread-malformed");

      act(() => {
        emitEvent({
          event_id: 1,
          event: "generation.memory_proposed",
          job_id: "job-malformed",
          thread_id: "thread-malformed",
          data: { proposed_memories: ["Orphaned."] },
        });
        emitEvent({
          event_id: 2,
          event: "generation.memory_proposed",
          job_id: "job-malformed",
          thread_id: "thread-malformed",
          data: { assistant_message_id: "assistant-6", proposed_memories: "Likes tea." },
        });
        emitEvent({
          event_id: 3,
          event: "generation.completed",
          job_id: "job-malformed",
          thread_id: "thread-malformed",
          data: { assistant_message_id: "assistant-6" },
        });
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({});
    });

    it("takes the suggestions from the status fallback when the stream drops before completion", async () => {
      const generationStatus = vi.fn().mockResolvedValue({
        job_id: "job-status-propose",
        kind: "generation",
        status: "succeeded",
        sequence: 3,
        result: { assistant_message_id: "assistant-7", proposed_memories: ["Likes tea."] },
      });
      const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
      const onCompleted = vi.fn().mockResolvedValue(undefined);

      act(() => {
        result.current.start("job-status-propose", "thread-status-propose", onCompleted, vi.fn());
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-7": ["Likes tea."] });
    });

    it("offers nothing when the job failed, whatever its result carried", async () => {
      const generationStatus = vi.fn().mockResolvedValue({
        job_id: "job-status-failed",
        kind: "generation",
        status: "failed",
        sequence: 3,
        error: "Generation failed.",
        result: { assistant_message_id: "assistant-8", proposed_memories: ["Must not appear."] },
      });
      const streamGeneration = vi.fn().mockRejectedValue(new Error("connection dropped"));
      const api = fakeApi({ streamGeneration, generationStatus });
      const { result } = renderHook(() => useGenerationStream(api, ignoreSessionExpiry));
      const onCompleted = vi.fn().mockResolvedValue(undefined);

      act(() => {
        result.current.start("job-status-failed", "thread-status-failed", onCompleted, vi.fn());
      });

      await waitFor(() => expect(onCompleted).toHaveBeenCalled());
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({});
    });
  });
});
