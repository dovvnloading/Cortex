import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readStoredModelJob, useModelStore, type ActiveModelJob } from "./useModelStore";

const KEY = "cortex.active.model-job";
const job: ActiveModelJob = { jobId: "job-1", kind: "gguf_download", model: "demo.Q4_K_M.gguf", lastEventId: 0 };

describe("the model job a tab is following", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
  });

  it("is remembered in session storage so a reload can find it again", () => {
    useModelStore.getState().setActiveJob(job);

    expect(useModelStore.getState().activeJob).toEqual(job);
    expect(JSON.parse(window.sessionStorage.getItem(KEY) ?? "null")).toEqual(job);
    expect(readStoredModelJob()).toEqual(job);
  });

  it("is forgotten in both places when it ends", () => {
    useModelStore.getState().setActiveJob(job);
    useModelStore.getState().setActiveJob(null);

    expect(useModelStore.getState().activeJob).toBeNull();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("moves its cursor forward only, and only for the job that is being followed", () => {
    useModelStore.getState().setActiveJob(job);

    useModelStore.getState().advanceActiveJob("job-1", 5);
    expect(readStoredModelJob()).toMatchObject({ jobId: "job-1", lastEventId: 5 });

    useModelStore.getState().advanceActiveJob("job-1", 3);
    useModelStore.getState().advanceActiveJob("job-1", 5);
    useModelStore.getState().advanceActiveJob("another-job", 9);
    expect(useModelStore.getState().activeJob?.lastEventId).toBe(5);
    expect(readStoredModelJob()?.lastEventId).toBe(5);

    useModelStore.getState().setActiveJob(null);
    useModelStore.getState().advanceActiveJob("job-1", 8);
    expect(useModelStore.getState().activeJob).toBeNull();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it.each([
    ["text that is not JSON", "not json"],
    ["a JSON value that is not an object", "42"],
    ["a job without an id", JSON.stringify({ ...job, jobId: "" })],
    ["a job of an unknown kind", JSON.stringify({ ...job, kind: "generation" })],
    ["a cursor that is not a whole number", JSON.stringify({ ...job, lastEventId: 1.5 })],
    ["a negative cursor", JSON.stringify({ ...job, lastEventId: -1 })],
    ["a label that is not text", JSON.stringify({ ...job, model: 7 })],
  ])("reads back nothing from %s", (_description, raw) => {
    window.sessionStorage.setItem(KEY, raw);
    expect(readStoredModelJob()).toBeNull();
  });

  it("keeps following a job when the browser refuses to store it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });

    expect(() => useModelStore.getState().setActiveJob(job)).not.toThrow();
    expect(() => useModelStore.getState().advanceActiveJob("job-1", 2)).not.toThrow();
    expect(useModelStore.getState().activeJob).toMatchObject({ jobId: "job-1", lastEventId: 2 });
    expect(readStoredModelJob()).toBeNull();
    expect(() => useModelStore.getState().setActiveJob(null)).not.toThrow();
    expect(useModelStore.getState().activeJob).toBeNull();
  });
});
