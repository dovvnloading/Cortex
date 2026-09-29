// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { SSEEvent } from "../../../contracts/cortex-api";
import { describeModelProgress } from "./modelJobs";

const event = (overrides: Partial<SSEEvent>): SSEEvent => ({
  id: 1,
  job_id: "job",
  kind: "progress",
  status: "running",
  phase: null,
  data: {},
  ...overrides,
});

const GIB = 1024 ** 3;

describe("describeModelProgress", () => {
  it("ignores events that are not progress", () => {
    expect(describeModelProgress(event({ kind: "state", status: "cancelling" }), "label")).toBeNull();
    expect(describeModelProgress(event({ kind: "completed", status: "succeeded" }), "label")).toBeNull();
    expect(describeModelProgress(event({ kind: "error", status: "failed" }), "label")).toBeNull();
  });

  it("keeps the label a download was started under, since its events name a file, not a model", () => {
    const progress = describeModelProgress(
      event({ phase: "gguf_download", data: { message: "downloading", filename: "other.gguf", completed: GIB, total: 4 * GIB, percent: 25 } }),
      "demo.Q4_K_M.gguf",
    );
    expect(progress).toEqual({ model: "demo.Q4_K_M.gguf", status: "Downloading (1.0 GB of 4.0 GB)", percent: 25 });
  });

  it.each([
    ["starting", { filename: "a.gguf" }, "Starting the download…"],
    ["downloading", { completed: 512, total: null }, "Downloading (512 B)"],
    ["downloading", {}, "Downloading"],
    ["retrying", { completed: 2 * GIB, total: 4 * GIB }, "Connection interrupted. Trying again (2.0 GB of 4.0 GB)"],
    ["success", { completed: 4 * GIB, total: 4 * GIB, percent: 100 }, "Download finished. Finishing up…"],
  ])("words a %s download update", (message, data, expected) => {
    const progress = describeModelProgress(event({ phase: "gguf_download", data: { message, ...data } }), "a.gguf");
    expect(progress?.status).toBe(expected);
  });

  it("shows an Ollama pull as the runtime reported it", () => {
    expect(
      describeModelProgress(
        event({ phase: "model_pull", data: { message: "pulling manifest", model: "qwen3:8b", percent: 42 } }),
        "local model inventory",
      ),
    ).toEqual({ model: "qwen3:8b", status: "pulling manifest", percent: 42 });
  });

  it("falls back to the phase, then to a plain word, and drops a percentage that is not a number", () => {
    expect(describeModelProgress(event({ phase: "model_check", data: {} }), "inventory")).toEqual({
      model: "inventory",
      status: "model_check",
      percent: null,
    });
    expect(describeModelProgress(event({ phase: null, data: { percent: "half" } }), "inventory")).toEqual({
      model: "inventory",
      status: "Working",
      percent: null,
    });
  });
});
