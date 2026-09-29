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

describe("resolveRuntimeAvailability while the model inventory is loading", () => {
  const loading = (selectedModel: string | null, overrides: Partial<Parameters<typeof resolveRuntimeAvailability>[0]> = {}) =>
    resolveRuntimeAvailability({
      selectedModel,
      // What the app computes from the empty stand-in inventory.
      selectedModelAvailable: false,
      inventoryLoading: true,
      ollamaConnected: true,
      llamacppStatus: { state: "idle" },
      ...overrides,
    });

  it("reports a neutral checking message instead of calling the model unavailable", () => {
    expect(loading("qwen3:8b")).toEqual({ ready: false, reason: "inventory-loading", message: "Checking local models…" });
    expect(loading("gguf:demo.gguf").reason).toBe("inventory-loading");
  });

  it("does not let a stale runtime verdict speak for the inventory that has not answered", () => {
    expect(loading("gguf:demo.gguf", { llamacppStatus: { state: "failed", last_error: "Vulkan device lost." } }).reason).toBe("inventory-loading");
    expect(loading("qwen3:8b", { ollamaConnected: false, ollamaMessage: "Ollama is not running." }).reason).toBe("inventory-loading");
  });

  it("still asks for a model when none is selected", () => {
    expect(loading(null).reason).toBe("no-model-selected");
  });

  it("reports a real unavailable model once the inventory has answered", () => {
    const answered = resolveRuntimeAvailability({
      selectedModel: "qwen3:8b",
      selectedModelAvailable: false,
      inventoryLoading: false,
      ollamaConnected: true,
      llamacppStatus: { state: "idle" },
    });

    expect(answered.reason).toBe("model-unavailable");
    expect(answered.message).toContain("unavailable");
  });

  it("is off by default, so existing callers keep their behaviour", () => {
    const result = resolveRuntimeAvailability({
      selectedModel: "qwen3:8b",
      selectedModelAvailable: true,
      ollamaConnected: true,
      llamacppStatus: { state: "idle" },
    });

    expect(result).toEqual({ ready: true, reason: null, message: null });
  });
});
