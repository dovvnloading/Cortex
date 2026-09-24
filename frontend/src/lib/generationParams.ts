import type { GenerationOptionsOverride, GenerationSettings } from "../../../contracts/cortex-api";

/** The numeric knobs a chat may override per request (GENERATION_OVERRIDE_FIELDS). */
export type ParamKey = keyof GenerationOptionsOverride;
export type SamplingKey = "temperature" | "top_p" | "top_k" | "repeat_penalty";
export type GenerationValues = Record<ParamKey, number>;

/**
 * Mirrors `GenerationSettings` in backend/cortex_backend/core/settings.py.
 * The backend fills any field the saved settings leave out with these same
 * values, so the UI must too -- otherwise a partial settings document would
 * show one number while the model received another.
 */
export const GENERATION_DEFAULTS: GenerationValues = {
  temperature: 0.7,
  top_p: 0.9,
  top_k: 40,
  repeat_penalty: 1.1,
  num_ctx: 8192,
  seed: -1,
};

export type ParamSpec = {
  key: ParamKey;
  label: string;
  description: string;
  min: number;
  max: number;
  /** Slider travel. Typed values may be finer than this. */
  step: number;
  /** Digits kept for typed values and shown in the readout. */
  decimals: number;
};

/** Bounds are the backend's validation limits; a value outside them is rejected on save. */
export const PARAM_SPECS: Record<ParamKey, ParamSpec> = {
  temperature: {
    key: "temperature",
    label: "Temperature",
    description: "Higher is more varied, lower is more focused.",
    min: 0,
    max: 2,
    step: 0.05,
    decimals: 2,
  },
  top_p: {
    key: "top_p",
    label: "Top P",
    description: "Keeps sampling to the most probable tokens.",
    min: 0,
    max: 1,
    step: 0.05,
    decimals: 2,
  },
  top_k: {
    key: "top_k",
    label: "Top K",
    description: "Candidate tokens to consider. 0 means no limit.",
    min: 0,
    max: 200,
    step: 1,
    decimals: 0,
  },
  repeat_penalty: {
    key: "repeat_penalty",
    label: "Repeat penalty",
    description: "Discourages repeated phrases. 1.00 turns it off.",
    min: 0.5,
    max: 2,
    step: 0.05,
    decimals: 2,
  },
  num_ctx: {
    key: "num_ctx",
    label: "Context window",
    description: "Bigger windows remember more and use more memory.",
    min: 2048,
    max: 65536,
    step: 1024,
    decimals: 0,
  },
  seed: {
    key: "seed",
    label: "Seed",
    description: "Empty for varied replies; set a number to repeat them.",
    min: -1,
    max: 2147483647,
    step: 1,
    decimals: 0,
  },
};

/** Common context sizes, offered as one-tap choices. All sit inside the backend bounds. */
export const CONTEXT_STOPS = [2048, 4096, 8192, 16384, 32768, 65536] as const;

export type PresetId = "precise" | "balanced" | "creative";
export type Preset = {
  id: PresetId;
  label: string;
  description: string;
  values: Pick<GenerationValues, "temperature" | "top_p" | "top_k">;
};

/**
 * Starting points for the three sampling knobs people actually reach for.
 * Balanced is the backend default, so a fresh install reads as Balanced.
 * Repeat penalty is left alone: it fixes a model-specific problem rather than
 * setting a tone, and a preset silently undoing that fix would be a surprise.
 */
export const PRESETS: readonly Preset[] = [
  { id: "precise", label: "Precise", description: "Focused, repeatable answers", values: { temperature: 0.2, top_p: 0.8, top_k: 20 } },
  { id: "balanced", label: "Balanced", description: "Cortex's default mix", values: { temperature: 0.7, top_p: 0.9, top_k: 40 } },
  { id: "creative", label: "Creative", description: "Looser, more varied wording", values: { temperature: 1, top_p: 0.95, top_k: 80 } },
];

const EPSILON = 1e-9;

function sameValue(left: number, right: number): boolean {
  return Math.abs(left - right) < EPSILON;
}

function finiteOr(value: number | null | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Effective values: per-chat override, else saved default, else backend default. */
export function resolveGenerationValues(
  defaults: GenerationSettings | null | undefined,
  override?: GenerationOptionsOverride | null,
): GenerationValues {
  const resolved = {} as GenerationValues;
  for (const key of Object.keys(GENERATION_DEFAULTS) as ParamKey[]) {
    const base = finiteOr(defaults?.[key], GENERATION_DEFAULTS[key]);
    resolved[key] = finiteOr(override?.[key], base);
  }
  return resolved;
}

export function matchingPreset(values: Pick<GenerationValues, "temperature" | "top_p" | "top_k">): PresetId | null {
  const preset = PRESETS.find((candidate) => (
    sameValue(candidate.values.temperature, values.temperature)
    && sameValue(candidate.values.top_p, values.top_p)
    && sameValue(candidate.values.top_k, values.top_k)
  ));
  return preset?.id ?? null;
}

/**
 * Keep only the fields that actually differ from the chat's defaults.
 * Returns null when nothing differs, so "no override" has one representation
 * and the composer's modified indicator can't light up for a value that is
 * already the default.
 */
export function normalizeOverride(
  override: GenerationOptionsOverride | null | undefined,
  defaults: GenerationValues,
): GenerationOptionsOverride | null {
  if (!override) return null;
  const next: GenerationOptionsOverride = {};
  let changed = false;
  for (const key of Object.keys(GENERATION_DEFAULTS) as ParamKey[]) {
    const value = override[key];
    if (typeof value !== "number" || !Number.isFinite(value) || sameValue(value, defaults[key])) continue;
    next[key] = value;
    changed = true;
  }
  return changed ? next : null;
}

/** "8K" for whole kibi-token sizes, otherwise the exact count. */
export function formatContextShort(tokens: number): string {
  return tokens % 1024 === 0 ? `${tokens / 1024}K` : tokens.toLocaleString();
}
