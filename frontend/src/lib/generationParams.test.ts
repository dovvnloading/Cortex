import { describe, expect, it } from "vitest";
import openapi from "../../../contracts/openapi.json";
import {
  CONTEXT_STOPS,
  GENERATION_DEFAULTS,
  PARAM_SPECS,
  PRESETS,
  formatContextShort,
  matchingPreset,
  normalizeOverride,
  resolveGenerationValues,
  type ParamKey,
} from "./generationParams";

type SchemaProperty = { default?: number; minimum?: number; maximum?: number; anyOf?: SchemaProperty[] };
const schemas = (openapi as unknown as { components: { schemas: Record<string, { properties: Record<string, SchemaProperty> }> } }).components.schemas;
const PARAM_KEYS = Object.keys(GENERATION_DEFAULTS) as ParamKey[];

describe("generation parameter limits", () => {
  // The UI must never offer a value the backend rejects on save, or show a
  // default the backend doesn't actually use. The generated contract is the
  // backend's own statement of both.
  it.each(PARAM_KEYS)("%s matches the backend's default and bounds", (key) => {
    const settings = schemas.GenerationSettings?.properties[key];
    const override = schemas.GenerationOptionsOverride?.properties[key]?.anyOf?.find((option) => option.minimum !== undefined);

    expect(settings?.default).toBe(GENERATION_DEFAULTS[key]);
    expect(settings?.minimum).toBe(PARAM_SPECS[key].min);
    expect(settings?.maximum).toBe(PARAM_SPECS[key].max);
    expect(override?.minimum).toBe(PARAM_SPECS[key].min);
    expect(override?.maximum).toBe(PARAM_SPECS[key].max);
  });

  it("only offers presets and context sizes inside those bounds", () => {
    for (const preset of PRESETS) {
      for (const [key, value] of Object.entries(preset.values) as [ParamKey, number][]) {
        expect(value).toBeGreaterThanOrEqual(PARAM_SPECS[key].min);
        expect(value).toBeLessThanOrEqual(PARAM_SPECS[key].max);
      }
    }
    for (const tokens of CONTEXT_STOPS) {
      expect(tokens).toBeGreaterThanOrEqual(PARAM_SPECS.num_ctx.min);
      expect(tokens).toBeLessThanOrEqual(PARAM_SPECS.num_ctx.max);
    }
  });

  it("reads the backend default as the Balanced preset", () => {
    expect(matchingPreset(GENERATION_DEFAULTS)).toBe("balanced");
  });
});

describe("resolveGenerationValues", () => {
  it("layers the chat override over saved defaults over backend defaults", () => {
    expect(resolveGenerationValues({ temperature: 0.4, num_ctx: 4096 }, { temperature: 1.2, seed: 9 })).toEqual({
      ...GENERATION_DEFAULTS,
      temperature: 1.2,
      num_ctx: 4096,
      seed: 9,
    });
  });

  it("ignores null and non-finite entries instead of passing them through", () => {
    expect(resolveGenerationValues({ top_k: Number.NaN }, { temperature: null })).toEqual(GENERATION_DEFAULTS);
    expect(resolveGenerationValues(null)).toEqual(GENERATION_DEFAULTS);
  });
});

describe("normalizeOverride", () => {
  it("keeps only fields that differ from the defaults", () => {
    expect(normalizeOverride({ temperature: 0.7, top_p: 0.5, seed: null }, GENERATION_DEFAULTS)).toEqual({ top_p: 0.5 });
  });

  it("collapses an override that changes nothing to null", () => {
    expect(normalizeOverride({ temperature: 0.7, num_ctx: 8192 }, GENERATION_DEFAULTS)).toBeNull();
    expect(normalizeOverride({}, GENERATION_DEFAULTS)).toBeNull();
    expect(normalizeOverride(null, GENERATION_DEFAULTS)).toBeNull();
  });

  it("treats float noise from slider arithmetic as equal", () => {
    expect(normalizeOverride({ temperature: 0.1 + 0.6 }, GENERATION_DEFAULTS)).toBeNull();
  });
});

describe("formatContextShort", () => {
  it("shortens whole kibi-token context sizes and spells out the rest", () => {
    expect(formatContextShort(8192)).toBe("8K");
    expect(formatContextShort(65536)).toBe("64K");
    expect(formatContextShort(10000)).toBe((10000).toLocaleString());
  });
});
