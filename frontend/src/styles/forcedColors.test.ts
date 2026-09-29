// @vitest-environment node
import { describe, expect, it } from "vitest";
import { declarationsOf, parseRules, selectorsOf, tokensCss } from "../test/css";

/**
 * Windows High Contrast drops box-shadows and author backgrounds, and this
 * stylesheet draws keyboard focus with a box-shadow in a dozen places. Focus
 * and control state cannot be exercised in jsdom (it evaluates no media
 * queries), so this pins the structure instead: any rule that removes a
 * focus outline must have a counterpart inside `@media (forced-colors: active)`
 * that puts a system-colour outline back. The behaviour itself is checked in a
 * real browser by e2e/accessibility.spec.ts.
 */

const FORCED = "(forced-colors: active)";

const rules = parseRules(tokensCss);
const forcedRules = rules.filter((rule) => rule.media === FORCED);

const removesOutline = (rule: (typeof rules)[number]) => {
  const outline = declarationsOf(rule).get("outline");
  return outline !== undefined && /^(0|none)\b/.test(outline);
};

/** Selector -> the selector that carries its focus indication in forced-colors mode. */
const forcedCounterparts = new Map<string, string>();
for (const rule of forcedRules) {
  const outline = declarationsOf(rule).get("outline") ?? "";
  if (!/\bHighlight\b/.test(outline)) continue;
  for (const selector of selectorsOf(rule)) forcedCounterparts.set(selector, selector);
}

/**
 * Base rules whose outline removal is not answered by a same-named selector:
 * either the ring is drawn on a wrapper (so the wrapper is what the
 * forced-colors block has to outline), or the base rule strips the outline
 * unconditionally and the block restores it only for keyboard focus.
 */
const COUNTERPARTS: Record<string, string> = {
  ".sidebar-search input:focus": ".sidebar-search:focus-within",
  ".composer-surface textarea": ".composer-surface:has(textarea:focus-visible)",
  ".model-picker-search input": ".model-picker-search:focus-within",
  ".model-picker-empty": ".model-picker-empty:focus-visible",
  ".memory-list-item input": ".memory-list-item input:focus-visible",
};

/**
 * Popup surfaces that receive programmatic focus when they open. They are
 * already delimited by a border (which forced colours keep) and the thing the
 * person navigates is the highlighted item inside, which does get an outline.
 */
const CONTAINERS_WITHOUT_OWN_INDICATOR = new Set([".model-picker", ".params-popover", ".model-picker-list"]);

describe("forced-colors mode", () => {
  it("has a forced-colors block", () => {
    expect(forcedRules.length).toBeGreaterThan(0);
  });

  it("restores an outline for every rule that removes one", () => {
    const missing: string[] = [];
    for (const rule of rules) {
      if (rule.media !== null || !removesOutline(rule)) continue;
      for (const selector of selectorsOf(rule)) {
        if (CONTAINERS_WITHOUT_OWN_INDICATOR.has(selector)) continue;
        const counterpart = COUNTERPARTS[selector] ?? selector;
        if (!forcedCounterparts.has(counterpart)) missing.push(`${selector} -> ${counterpart}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("keeps its exemption and mapping tables pointing at rules that still exist", () => {
    const baseSelectors = new Set(rules.filter((rule) => rule.media === null).flatMap(selectorsOf));
    for (const original of [...Object.keys(COUNTERPARTS), ...CONTAINERS_WITHOUT_OWN_INDICATOR]) {
      expect(baseSelectors.has(original), `${original} no longer exists in tokens.css`).toBe(true);
    }
  });

  it("outlines with a system colour, never an author colour", () => {
    for (const rule of forcedRules) {
      const outline = declarationsOf(rule).get("outline");
      if (outline === undefined) continue;
      expect(outline, rule.selector).toMatch(/\b(Highlight|ButtonText|CanvasText)\b/);
      expect(outline, rule.selector).not.toMatch(/var\(|#|rgb/);
    }
  });

  it("draws the checked toggle from system colours and marks it with a glyph", () => {
    const toggleRules = forcedRules.filter((rule) => selectorsOf(rule).some((selector) => selector.startsWith('.toggle-row input[type="checkbox"]')));
    const byPseudo = (selector: string) => toggleRules.find((rule) => selectorsOf(rule).includes(selector));

    expect(declarationsOf(byPseudo('.toggle-row input[type="checkbox"]')!).get("forced-color-adjust")).toBe("none");
    expect(declarationsOf(byPseudo('.toggle-row input[type="checkbox"]:checked')!).get("background")).toBe("Highlight");
    expect(declarationsOf(byPseudo('.toggle-row input[type="checkbox"]:checked::before')!).get("content")).toBe('"✓"');
    // The thumb has to differ from the track in both states or it disappears.
    expect(declarationsOf(byPseudo('.toggle-row input[type="checkbox"]::after')!).get("background")).toBe("ButtonText");
    expect(declarationsOf(byPseudo('.toggle-row input[type="checkbox"]:checked::after')!).get("background")).toBe("HighlightText");
  });

  it("repaints the slider track with system colours", () => {
    const track = forcedRules.find((rule) => selectorsOf(rule).includes(".range-input::-webkit-slider-runnable-track"));
    const background = declarationsOf(track!).get("background") ?? "";
    expect(background).toContain("Highlight");
    expect(background).toContain("ButtonText");
    expect(background).not.toMatch(/var\(--(accent|line)/);
  });

  it("marks the current row, tab, segment and model with an outline", () => {
    for (const selector of [".chat-row-active", ".settings-tab-active", ".segmented-option-checked", ".model-choice-selected"]) {
      expect(forcedCounterparts.has(selector), selector).toBe(true);
    }
  });
});
