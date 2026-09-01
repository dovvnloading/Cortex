import type { LlamaCppRuntimeStatus } from "../../../contracts/cortex-api";
import { isGGUFModel } from "../lib/localModels";

export type RuntimeDisableReason =
  | "no-model-selected"
  | "model-unavailable"
  | "ollama-unavailable"
  | "gguf-runtime-failed";

export type RuntimeAvailability = {
  ready: boolean;
  reason: RuntimeDisableReason | null;
  message: string | null;
};

export function resolveRuntimeAvailability({
  selectedModel,
  selectedModelAvailable,
  ollamaConnected,
  ollamaMessage,
  llamacppStatus,
}: {
  selectedModel: string | null;
  selectedModelAvailable: boolean;
  ollamaConnected: boolean;
  ollamaMessage?: string | null;
  llamacppStatus: LlamaCppRuntimeStatus;
}): RuntimeAvailability {
  if (!selectedModel) {
    return { ready: false, reason: "no-model-selected", message: "Select a local model before sending a message." };
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
    return { ready: true, reason: null, message: null };
  }
  if (!ollamaConnected) {
    return { ready: false, reason: "ollama-unavailable", message: ollamaMessage ?? "Ollama is unavailable. Start Ollama and rescan local models." };
  }
  return { ready: true, reason: null, message: null };
}
