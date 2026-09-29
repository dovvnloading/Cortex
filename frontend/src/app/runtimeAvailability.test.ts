import { describe, expect, it } from "vitest";
import type { LlamaCppRuntimeStatus } from "../../../contracts/cortex-api";
import { resolveRuntimeAvailability } from "./runtimeAvailability";

const RESTART_MESSAGE = "The local model runtime did not exit cleanly; restart Cortex before trying again.";

function availabilityFor(selectedModel: string, llamacppStatus: LlamaCppRuntimeStatus) {
  return resolveRuntimeAvailability({
    selectedModel,
    selectedModelAvailable: true,
    ollamaConnected: false,
    ollamaMessage: "Ollama is not running.",
    llamacppStatus,
  });
}

describe("resolveRuntimeAvailability for a GGUF model", () => {
  it("blocks sending while the runtime is parked in stopping with an error", () => {
    expect(availabilityFor("gguf:demo.gguf", { state: "stopping", last_error: RESTART_MESSAGE })).toEqual({
      ready: false,
      reason: "gguf-runtime-needs-restart",
      message: RESTART_MESSAGE,
    });
  });

  it("does not block sending for an ordinary teardown in flight", () => {
    for (const status of [
      { state: "stopping" },
      { state: "stopping", last_error: null },
      { state: "stopping", last_error: "" },
    ] satisfies LlamaCppRuntimeStatus[]) {
      expect(availabilityFor("gguf:demo.gguf", status)).toEqual({ ready: true, reason: null, message: null });
    }
  });

  it("still reports a failed runtime with its own error, or a default one", () => {
    expect(availabilityFor("gguf:demo.gguf", { state: "failed", last_error: "Vulkan device lost." })).toEqual({
      ready: false,
      reason: "gguf-runtime-failed",
      message: "Vulkan device lost.",
    });
    expect(availabilityFor("gguf:demo.gguf", { state: "failed" }).message).toBe(
      "The local GGUF runtime failed to start. Check System settings and try again.",
    );
  });

  it("leaves the other states sendable", () => {
    for (const state of ["idle", "downloading_binary", "starting", "ready"] as const) {
      expect(availabilityFor("gguf:demo.gguf", { state, last_error: null }).ready).toBe(true);
    }
  });

  it("does not let a stopping GGUF runtime affect an Ollama model", () => {
    const status: LlamaCppRuntimeStatus = { state: "stopping", last_error: RESTART_MESSAGE };

    expect(
      resolveRuntimeAvailability({
        selectedModel: "qwen3:8b",
        selectedModelAvailable: true,
        ollamaConnected: true,
        llamacppStatus: status,
      }),
    ).toEqual({ ready: true, reason: null, message: null });
  });
});
