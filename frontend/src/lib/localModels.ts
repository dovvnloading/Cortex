import type { ModelResponse } from "../../../contracts/cortex-api";

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

export function formatModelSize(size: number | null | undefined): string | null {
  if (!size || size < 1) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  const value = size / (1024 ** index);
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}
