import type { LlamaCppRuntimeStatus } from "../../../contracts/cortex-api";
import { isGGUFModel } from "../lib/localModels";

export type RuntimeDisableReason =
  | "no-model-selected"
  | "inventory-loading"
  | "model-unavailable"
  | "ollama-unavailable"
  | "gguf-runtime-failed"
  | "gguf-runtime-needs-restart";

export type RuntimeAvailability = {
  ready: boolean;
  reason: RuntimeDisableReason | null;
  message: string | null;
};

export function resolveRuntimeAvailability({
  selectedModel,
  selectedModelAvailable,
  inventoryLoading = false,
  ollamaConnected,
  ollamaMessage,
  llamacppStatus,
}: {
  selectedModel: string | null;
  selectedModelAvailable: boolean;
  /**
   * The local model inventory has not answered yet. What is installed is not
   * known, so the selected model is neither available nor unavailable -- and
   * saying "unavailable" on every launch reported a fault that was not there.
   */
  inventoryLoading?: boolean;
  ollamaConnected: boolean;
  ollamaMessage?: string | null;
  llamacppStatus: LlamaCppRuntimeStatus;
}): RuntimeAvailability {
  if (!selectedModel) {
    return { ready: false, reason: "no-model-selected", message: "Select a local model before sending a message." };
  }
  if (inventoryLoading) {
    return { ready: false, reason: "inventory-loading", message: "Checking local models…" };
  }
  if (!selectedModelAvailable) {
    return { ready: false, reason: "model-unavailable", message: "The selected local model is unavailable. Choose an installed model and try again." };
  }
  if (isGGUFModel(selectedModel)) {
    if (llamacppStatus.state === "failed") {
      return {
        ready: false,
        reason: "gguf-runtime-failed",
        message: llamacppStatus.last_error ?? "The local GGUF runtime failed to start. Check System settings and try again.",
      };
    }
    // The manager parks in "stopping" with an error when it could not confirm
    // that the old runtime process exited, and stays there until Cortex is
    // restarted: every send would fail. A plain "stopping" (no error) is a
    // teardown in flight and resolves by itself, so it does not block sending.
    if (llamacppStatus.state === "stopping" && llamacppStatus.last_error) {
      return {
        ready: false,
        reason: "gguf-runtime-needs-restart",
        message: llamacppStatus.last_error,
      };
    }
    return { ready: true, reason: null, message: null };
  }
  if (!ollamaConnected) {
    return { ready: false, reason: "ollama-unavailable", message: ollamaMessage ?? "Ollama is unavailable. Start Ollama and rescan local models." };
  }
  return { ready: true, reason: null, message: null };
}
