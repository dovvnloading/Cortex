import { act, render, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobAccepted, JobStatusResponse, ModelResponse, SSEEvent } from "../../../contracts/cortex-api";
import { ApiError, type CortexApi } from "../api/client";
import { useModelStore, type ActiveModelJob } from "../stores/useModelStore";
import { useModelJobs, type ModelJobResult } from "./useModelJobs";

const STORAGE_KEY = "cortex.active.model-job";
const GIB = 1024 ** 3;

const inventory: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: ["gguf:demo.Q4_K_M.gguf"],
  connection: { success: true, status: "connected", message: "Ready" },
};

type Stream = {
  jobId: string;
  options: { signal?: AbortSignal; afterEventId?: number } | undefined;
  emit: (event: SSEEvent) => void;
  /** Deliver the terminal event and close the stream, as the backend does. */
  finish: (event: SSEEvent) => void;
  /** Close the stream without a terminal event. */
  close: () => void;
  fail: (error: unknown) => void;
};

function event(id: number, overrides: Partial<SSEEvent> = {}): SSEEvent {
  return { id, job_id: "job", kind: "progress", status: "running", phase: null, data: {}, ...overrides };
}

const downloading = (id: number, completed: number, total: number) => event(id, {
  phase: "gguf_download",
  data: { message: "downloading", filename: "demo.Q4_K_M.gguf", completed, total, percent: Math.round((completed / total) * 100) },
});

const succeeded = (id: number, data: Record<string, unknown> = { filename: "demo.Q4_K_M.gguf" }) => (
  event(id, { kind: "completed", status: "succeeded", data })
);

function accepted(jobId: string, kind: JobAccepted["kind"] = "gguf_download"): JobAccepted {
  return { job_id: jobId, kind, status: "queued" };
}

function job(overrides: Partial<ActiveModelJob> = {}): ActiveModelJob {
  return { jobId: "job-1", kind: "gguf_download", model: "demo.Q4_K_M.gguf", lastEventId: 0, ...overrides };
}

function fakes() {
  const streams: Stream[] = [];
  const api = {
    streamJob: vi.fn((jobId: string, onEvent: (event: SSEEvent) => void, streamOptions?: Stream["options"]) => (
      new Promise<SSEEvent | null>((resolve, reject) => {
        streamOptions?.signal?.addEventListener("abort", () => reject(new ApiError(0, "The request was cancelled.", "aborted")), { once: true });
        streams.push({
          jobId,
          options: streamOptions,
          emit: onEvent,
          finish: (terminal) => { onEvent(terminal); resolve(terminal); },
          close: () => resolve(null),
          fail: reject,
        });
      })
    )),
    jobStatus: vi.fn<(jobId: string) => Promise<JobStatusResponse>>(),
    models: vi.fn<() => Promise<ModelResponse>>().mockResolvedValue(inventory),
    cancelJob: vi.fn<(jobId: string) => Promise<JobStatusResponse>>().mockResolvedValue({} as JobStatusResponse),
  };
  const notify = vi.fn();
  const onModels = vi.fn();
  const onDownloaded = vi.fn<(filename: string) => Promise<void>>().mockResolvedValue(undefined);
  return { api, streams, notify, onModels, onDownloaded };
}

function setup() {
  const context = fakes();
  const { api, notify, onModels, onDownloaded } = context;
  const hook = renderHook(() => useModelJobs({ api: api as unknown as CortexApi, notify, onModels, onDownloaded }));
  return { ...context, ...hook };
}

function Host({ context }: { context: ReturnType<typeof fakes> }) {
  const { api, notify, onModels, onDownloaded } = context;
  useModelJobs({ api: api as unknown as CortexApi, notify, onModels, onDownloaded });
  return null;
}

const state = () => useModelStore.getState();
const remembered = () => JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? "null") as ActiveModelJob | null;

afterEach(() => window.sessionStorage.clear());

describe("following a job the backend just accepted", () => {
  it("shows its progress, remembers it while it runs, and forgets it once it has succeeded", async () => {
    const { result, streams, notify, onModels, api } = setup();

    let outcome!: Promise<ModelJobResult>;
    act(() => {
      outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf", { checkOllamaConnection: false, notifyOnSuccess: false });
    });
    await waitFor(() => expect(streams).toHaveLength(1));

    expect(streams[0].options?.afterEventId).toBeUndefined();
    expect(state().modelBusy).toBe(true);
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Starting...", percent: null });
    expect(state().activeJob).toEqual(job());
    expect(remembered()).toEqual(job());

    act(() => streams[0].emit(downloading(3, GIB, 4 * GIB)));
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Downloading (1.0 GB of 4.0 GB)", percent: 25 });
    expect(remembered()?.lastEventId).toBe(3);

    await act(async () => streams[0].finish(succeeded(4)));
    const finished = await outcome;

    expect(finished).toEqual({ succeeded: true, data: { filename: "demo.Q4_K_M.gguf" }, failure: null, cancelled: false, detached: false });
    expect(api.models).toHaveBeenCalledTimes(1);
    expect(onModels).toHaveBeenCalledWith(inventory);
    expect(notify).not.toHaveBeenCalled();
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
    expect(remembered()).toBeNull();
  });

  it("announces success unless the caller words it itself", async () => {
    const { result, streams, notify } = setup();

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-2", "models"), "qwen3:8b"); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].finish(succeeded(2, {})));
    await outcome;

    expect(notify).toHaveBeenCalledWith("Model operation completed.", "success");
  });

  it("reports an unreachable Ollama when asked to check for it", async () => {
    const { result, streams, notify, api } = setup();
    api.models.mockResolvedValue({ ...inventory, connection: { success: false, status: "error", message: "Ollama is not running." } });

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-3", "models")); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].finish(succeeded(2, {})));

    expect((await outcome).succeeded).toBe(true);
    expect(notify).toHaveBeenCalledWith("Ollama is not running.", "error");
    expect(notify).not.toHaveBeenCalledWith("Local model inventory refreshed.", "success");
  });

  it("gives the backend's own sentence when the job fails, and forgets the job", async () => {
    const { result, streams, notify, onModels } = setup();

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].finish(event(2, { kind: "error", status: "failed", data: { message: "There is not enough free disk space." } })));

    expect(await outcome).toEqual({ succeeded: false, data: null, failure: "There is not enough free disk space.", cancelled: false, detached: false });
    expect(notify).toHaveBeenCalledWith("There is not enough free disk space.", "error");
    expect(onModels).not.toHaveBeenCalled();
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
    expect(remembered()).toBeNull();
  });

  it("reconciles a stream that closed without saying how the job ended", async () => {
    const { result, streams, api, onModels } = setup();
    api.jobStatus.mockResolvedValue({ job_id: "job-1", kind: "gguf_download", status: "succeeded", sequence: 5, result: { filename: "demo.Q4_K_M.gguf" } });

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf", { notifyOnSuccess: false }); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].close());

    expect(await outcome).toMatchObject({ succeeded: true, data: { filename: "demo.Q4_K_M.gguf" } });
    expect(api.jobStatus).toHaveBeenCalledWith("job-1");
    expect(onModels).toHaveBeenCalledTimes(1);
  });

  it("does not claim success, or forget the job, while it may still be running", async () => {
    const { result, streams, api, notify, onModels } = setup();
    api.jobStatus.mockResolvedValue({ job_id: "job-1", kind: "gguf_download", status: "running", sequence: 3 });

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].close());

    expect(await outcome).toMatchObject({ succeeded: false, failure: expect.stringContaining("completion was not confirmed") });
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("completion was not confirmed"), "error");
    expect(onModels).not.toHaveBeenCalled();
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toEqual(job());
  });

  it("reports a cancellation nobody asked for as a failure", async () => {
    const { result, streams, notify } = setup();

    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(streams).toHaveLength(1));
    await act(async () => streams[0].finish(event(2, { kind: "state", status: "cancelled", data: { message: "Job cancelled." } })));

    expect(await outcome).toMatchObject({ succeeded: false, cancelled: false, failure: "Model operation was cancelled." });
    expect(notify).toHaveBeenCalledWith("Model operation was cancelled.", "error");
  });
});

describe("cancelling", () => {
  async function running() {
    const context = setup();
    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = context.result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(context.streams).toHaveLength(1));
    act(() => context.streams[0].emit(downloading(2, GIB, 4 * GIB)));
    return { ...context, outcome };
  }

  it("asks the backend to stop the job, and reports it stopped once it has", async () => {
    const { result, streams, api, notify, onModels, outcome } = await running();

    await act(async () => { await result.current.cancel(); });

    expect(api.cancelJob).toHaveBeenCalledWith("job-1");
    expect(state().modelProgress).toMatchObject({ status: "Cancelling...", cancelling: true, percent: 25 });
    // Progress that was already on its way must not undo the wording.
    act(() => streams[0].emit(downloading(3, 2 * GIB, 4 * GIB)));
    expect(state().modelProgress).toMatchObject({ status: "Cancelling...", cancelling: true, percent: 50 });

    await act(async () => streams[0].finish(event(4, { kind: "state", status: "cancelled", data: { message: "Job cancelled." } })));

    expect(await outcome).toEqual({ succeeded: false, data: null, failure: null, cancelled: true, detached: false });
    expect(notify).toHaveBeenCalledWith("Download cancelled.", "info");
    expect(notify).not.toHaveBeenCalledWith(expect.anything(), "error");
    expect(onModels).not.toHaveBeenCalled();
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Cancelled.", percent: null });
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
  });

  it("asks only once, however often it is pressed", async () => {
    const { result, api } = await running();

    await act(async () => { await Promise.all([result.current.cancel(), result.current.cancel()]); });
    await act(async () => { await result.current.cancel(); });

    expect(api.cancelJob).toHaveBeenCalledTimes(1);
  });

  it("says so when the backend refuses, and puts the progress back so it can be tried again", async () => {
    const { result, api, notify } = await running();
    api.cancelJob.mockRejectedValueOnce(new ApiError(404, "Job not found."));

    await act(async () => { await result.current.cancel(); });

    expect(notify).toHaveBeenCalledWith("Job not found.", "error");
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Downloading (1.0 GB of 4.0 GB)", percent: 25 });

    await act(async () => { await result.current.cancel(); });
    expect(api.cancelJob).toHaveBeenCalledTimes(2);
  });

  it("does nothing when no job is being followed", async () => {
    const { result, api, notify } = setup();

    await act(async () => { await result.current.cancel(); });

    expect(api.cancelJob).not.toHaveBeenCalled();
    expect(notify).not.toHaveBeenCalled();
  });

  it("lets a job that finished before the cancel arrived count as finished", async () => {
    const { result, streams, outcome } = await running();

    await act(async () => { await result.current.cancel(); });
    await act(async () => streams[0].finish(succeeded(5)));

    expect(await outcome).toMatchObject({ succeeded: true, cancelled: false });
  });
});

describe("picking a job up again", () => {
  it("continues from the last event a previous screen showed, then finishes what the download's caller would have", async () => {
    state().setActiveJob(job({ lastEventId: 7 }));
    const { streams, notify, onModels, onDownloaded } = setup();

    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].jobId).toBe("job-1");
    expect(streams[0].options?.afterEventId).toBe(7);
    expect(state().modelBusy).toBe(true);
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Reconnecting...", percent: null });

    act(() => streams[0].emit(downloading(8, 3 * GIB, 4 * GIB)));
    expect(state().modelProgress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Downloading (3.0 GB of 4.0 GB)", percent: 75 });

    await act(async () => streams[0].finish(succeeded(9)));

    await waitFor(() => expect(onDownloaded).toHaveBeenCalledWith("demo.Q4_K_M.gguf"));
    expect(onModels).toHaveBeenCalledWith(inventory);
    expect(notify).not.toHaveBeenCalled();
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
    expect(remembered()).toBeNull();
  });

  it("finds the job in session storage after a reload, when the store starts empty", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(job({ jobId: "job-reloaded", lastEventId: 12 })));
    const { streams } = setup();

    await waitFor(() => expect(streams).toHaveLength(1));

    expect(streams[0].jobId).toBe("job-reloaded");
    expect(streams[0].options?.afterEventId).toBe(12);
    expect(state().activeJob).toEqual(job({ jobId: "job-reloaded", lastEventId: 12 }));
  });

  it("does not go looking for a job when nothing is remembered, or what is remembered is garbage", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, "{not json");
    const { streams, api } = setup();

    await Promise.resolve();

    expect(streams).toHaveLength(0);
    expect(api.streamJob).not.toHaveBeenCalled();
    expect(state().modelBusy).toBe(false);
  });

  it("tells the person about an Ollama pull that finished while nothing was watching", async () => {
    state().setActiveJob(job({ jobId: "pull-1", kind: "models", model: "qwen3:8b", lastEventId: 2 }));
    const { streams, notify, onDownloaded } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => streams[0].finish(succeeded(3, {})));

    await waitFor(() => expect(notify).toHaveBeenCalledWith("Model operation completed.", "success"));
    expect(onDownloaded).not.toHaveBeenCalled();
  });

  it("does not select anything for a download that failed while nothing was watching", async () => {
    state().setActiveJob(job({ lastEventId: 2 }));
    const { streams, notify, onDownloaded } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => streams[0].finish(event(3, { kind: "error", status: "failed", data: { message: "The download server refused access (HTTP 403)." } })));

    await waitFor(() => expect(notify).toHaveBeenCalledWith("The download server refused access (HTTP 403).", "error"));
    expect(onDownloaded).not.toHaveBeenCalled();
    expect(state().activeJob).toBeNull();
  });

  it("finishes a job that already ended from the events the backend kept, once", async () => {
    state().setActiveJob(job({ lastEventId: 1 }));
    const { streams, onDownloaded } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    // Nothing was shown of this job's ending, so its terminal event arrives at once.
    await act(async () => streams[0].finish(succeeded(2)));

    await waitFor(() => expect(onDownloaded).toHaveBeenCalledTimes(1));
    expect(streams).toHaveLength(1);
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
  });

  it("keeps one live stream for the job when StrictMode mounts, unmounts and mounts again", async () => {
    state().setActiveJob(job({ lastEventId: 3 }));
    const context = fakes();

    render(<StrictMode><Host context={context} /></StrictMode>);

    // Whatever the first mount attached is cut by the simulated unmount; the
    // last stream is the one that counts, and it finishes the job once.
    await waitFor(() => expect(context.streams.length).toBe(2));
    const live = context.streams.filter((stream) => stream.options?.signal?.aborted === false);
    expect(live).toHaveLength(1);
    expect(live[0].options?.afterEventId).toBe(3);
    await act(async () => live[0].finish(succeeded(4)));
    await waitFor(() => expect(context.onDownloaded).toHaveBeenCalledTimes(1));
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toBeNull();
  });

  it.each([
    [404, "Job not found."],
    [403, "Job does not belong to this session."],
  ])("drops, without a word, a job the backend answers %i for, and looks at the folder again", async (status, detail) => {
    state().setActiveJob(job({ lastEventId: 4 }));
    const { streams, notify, onModels, onDownloaded } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => streams[0].fail(new ApiError(status, detail)));

    await waitFor(() => expect(onModels).toHaveBeenCalledWith(inventory));
    expect(notify).not.toHaveBeenCalled();
    expect(onDownloaded).not.toHaveBeenCalled();
    expect(state().activeJob).toBeNull();
    expect(remembered()).toBeNull();
    expect(state().modelBusy).toBe(false);
  });

  it.each([
    ["the connection to the backend is lost", new ApiError(0, "Cortex could not reach the local backend.", "network")],
    ["the session has expired", new ApiError(401, "Local session expired.")],
  ])("keeps the job when %s, because that says nothing about the job", async (_description, error) => {
    state().setActiveJob(job({ lastEventId: 4 }));
    const { streams, notify } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => streams[0].fail(error));

    await waitFor(() => expect(notify).toHaveBeenCalledWith(error.detail, "error"));
    expect(state().modelBusy).toBe(false);
    expect(state().activeJob).toEqual(job({ lastEventId: 4 }));
    expect(remembered()).toEqual(job({ lastEventId: 4 }));
  });

  it("forgets the job after any other refusal, so it is not asked about forever", async () => {
    state().setActiveJob(job({ lastEventId: 4 }));
    const { streams, notify } = setup();
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => streams[0].fail(new ApiError(500, "The local workspace did not respond.")));

    await waitFor(() => expect(notify).toHaveBeenCalledWith("The local workspace did not respond.", "error"));
    expect(state().activeJob).toBeNull();
  });
});

describe("when the screen goes away", () => {
  it("cuts the stream and lets go of the row, but keeps the job for the next screen", async () => {
    const { result, streams, notify, onModels, unmount } = setup();
    let outcome!: Promise<ModelJobResult>;
    act(() => { outcome = result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => streams[0].emit(downloading(6, GIB, 4 * GIB)));

    unmount();

    expect(streams[0].options?.signal?.aborted).toBe(true);
    expect(await outcome).toMatchObject({ detached: true, succeeded: false, failure: null });
    expect(state().modelBusy).toBe(false);
    expect(state().modelProgress).toBeNull();
    expect(state().activeJob).toEqual(job({ lastEventId: 6 }));
    expect(remembered()).toEqual(job({ lastEventId: 6 }));
    expect(notify).not.toHaveBeenCalled();
    expect(onModels).not.toHaveBeenCalled();
  });

  it("is picked up again, from the same place, by the next screen", async () => {
    const first = setup();
    act(() => { void first.result.current.run(accepted("job-1"), "demo.Q4_K_M.gguf"); });
    await waitFor(() => expect(first.streams).toHaveLength(1));
    act(() => first.streams[0].emit(downloading(6, GIB, 4 * GIB)));
    first.unmount();

    const second = setup();

    await waitFor(() => expect(second.streams).toHaveLength(1));
    expect(second.streams[0].jobId).toBe("job-1");
    expect(second.streams[0].options?.afterEventId).toBe(6);
    expect(state().modelBusy).toBe(true);
  });

  it("only remembers a job that was accepted just as the screen closed", async () => {
    const { result, api, unmount } = setup();
    unmount();

    const outcome = await result.current.run(accepted("job-late"), "late.gguf");

    expect(outcome.detached).toBe(true);
    expect(api.streamJob).not.toHaveBeenCalled();
    expect(state().activeJob).toEqual(job({ jobId: "job-late", model: "late.gguf" }));
  });
});

describe("a second job while one is being followed", () => {
  it("is watched and reported, but leaves the row and Cancel to the first", async () => {
    const { result, streams, notify, onModels, api } = setup();
    act(() => { void result.current.run(accepted("download-1"), "demo.Q4_K_M.gguf", { notifyOnSuccess: false }); });
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => streams[0].emit(downloading(2, GIB, 4 * GIB)));

    let rescan!: Promise<ModelJobResult>;
    act(() => { rescan = result.current.run(accepted("rescan-1", "models")); });
    await waitFor(() => expect(streams).toHaveLength(2));
    act(() => streams[1].emit(event(1, { phase: "model_check", data: { message: "Checking local models." } })));

    expect(state().activeJob?.jobId).toBe("download-1");
    expect(state().modelProgress).toMatchObject({ model: "demo.Q4_K_M.gguf", percent: 25 });

    await act(async () => streams[1].finish(succeeded(2, {})));
    expect(await rescan).toMatchObject({ succeeded: true });
    expect(onModels).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenCalledWith("Local model inventory refreshed.", "success");
    // The rescan finishing must not release the download's busy flag or forget it.
    expect(state().modelBusy).toBe(true);
    expect(state().activeJob?.jobId).toBe("download-1");
    expect(remembered()?.jobId).toBe("download-1");

    await act(async () => { await result.current.cancel(); });
    expect(api.cancelJob).toHaveBeenCalledWith("download-1");
  });

  it("is cut with the first when the screen goes away", async () => {
    const { result, streams, unmount } = setup();
    act(() => { void result.current.run(accepted("download-1"), "demo.Q4_K_M.gguf"); });
    act(() => { void result.current.run(accepted("rescan-1", "models")); });
    await waitFor(() => expect(streams).toHaveLength(2));

    unmount();

    expect(streams.map((stream) => stream.options?.signal?.aborted)).toEqual([true, true]);
    expect(state().activeJob?.jobId).toBe("download-1");
  });
});
