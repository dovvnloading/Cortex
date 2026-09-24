import type { InstalledModel, ModelResponse } from "../../../contracts/cortex-api";

/**
 * Normalize the local model inventory returned by Cortex.
 *
 * `installed_models` is the compact compatibility field, while `models`
 * carries display metadata. Some older servers may only provide one of them,
 * so the UI combines both rather than treating an empty detail array as an
 * authoritative empty inventory.
 */
export function localModelNames(models: Pick<ModelResponse, "installed_models" | "models">): string[] {
  const names = [
    ...(models.installed_models ?? []),
    ...(models.models ?? []).map((model) => model.name),
  ]
    .map((model) => model.trim())
    .filter(Boolean);

  return Array.from(new Set(names)).sort((left, right) => left.localeCompare(right));
}

/** Ollama tags never contain this prefix, so it unambiguously identifies a
 * locally-scanned GGUF model id (see backend `llamacpp/model_directory.py`). */
export function isGGUFModel(name: string | null | undefined): boolean {
  return Boolean(name && name.startsWith("gguf:"));
}

/** Strip the internal "gguf:" id prefix for user-facing text -- the prefix
 * matters for backend routing, not for what a person reads in the UI. */
export function displayModelName(name: string): string {
  return isGGUFModel(name) ? name.slice("gguf:".length) : name;
}

export function formatModelSize(size: number | null | undefined): string | null {
  if (!size || size < 1) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  const value = size / (1024 ** index);
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

export type ModelSource = "ollama" | "gguf";

export function modelSource(name: string, detail?: Pick<InstalledModel, "source"> | null): ModelSource {
  return detail?.source ?? (isGGUFModel(name) ? "gguf" : "ollama");
}

/** The facts that tell two local models apart at a glance: size class, quantization, disk size. */
export function modelFacts(detail: InstalledModel | null | undefined): string[] {
  if (!detail) return [];
  const facts: string[] = [];
  if (detail.parameter_size) facts.push(detail.parameter_size);
  if (detail.quantization_level) facts.push(detail.quantization_level);
  const size = formatModelSize(detail.size);
  if (size) facts.push(size);
  return facts;
}
