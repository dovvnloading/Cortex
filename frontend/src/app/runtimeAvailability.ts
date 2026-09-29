import type { LlamaCppRuntimeStatus } from "../../../contracts/cortex-api";
import { isGGUFModel } from "../lib/localModels";

export type RuntimeDisableReason =
  | "no-model-selected"
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

/** How often the runtime is asked for its state while it is changing, or while a reply depends on it. */
export const LLAMACPP_ACTIVE_POLL_MS = 2000;
/** How often it is asked otherwise: a ready or idle runtime rarely changes on its own. */
export const LLAMACPP_IDLE_POLL_MS = 15_000;

/**
 * How often to poll the local GGUF runtime. A download, a start, and a stop
 * each move through states the user is waiting on, and a running generation is
 * what makes the runtime start, so those are watched closely. A runtime that
 * is ready, idle, or failed changes only when the user acts, which is far less
 * often than every two seconds for as long as a GGUF model stays selected.
 * "Stopping" with an error is the parked, needs-a-restart state: it does not
 * resolve by itself, so it is not watched closely either.
 */
export function llamacppPollInterval(
  status: LlamaCppRuntimeStatus | null | undefined,
  generationActive: boolean,
): number {
  if (generationActive) return LLAMACPP_ACTIVE_POLL_MS;
  switch (status?.state) {
    case "downloading_binary":
    case "starting":
      return LLAMACPP_ACTIVE_POLL_MS;
    case "stopping":
      return status.last_error ? LLAMACPP_IDLE_POLL_MS : LLAMACPP_ACTIVE_POLL_MS;
    default:
      return LLAMACPP_IDLE_POLL_MS;
  }
}
