// @vitest-environment node
import { describe, expect, it } from "vitest";
import { declarationsOf, parseRules, selectorsOf, tokensCss } from "../test/css";

/**
 * The colour tokens are the accessibility contract of the whole UI, and a
 * one-character edit to a hex value is invisible in review. These tests parse
 * the real stylesheet and compute WCAG contrast for the pairs the interface
 * actually draws, so a regression fails here instead of in someone's eyes.
 */

type Theme = "light" | "dark";
type Rgba = { r: number; g: number; b: number; a: number };

const AA_TEXT = 4.5;
/** 11px. The smallest text size the type scale allows (`--text-2xs`). */
const MIN_FONT_SIZE_REM = 0.6875;

const rules = parseRules(tokensCss);

function tokensOf(selector: string): Map<string, string> {
  const rule = rules.find((candidate) => candidate.media === null && candidate.selector === selector);
  if (!rule) throw new Error(`tokens.css has no ${selector} block`);
  const tokens = new Map<string, string>();
  for (const [name, value] of declarationsOf(rule)) {
    if (name.startsWith("--")) tokens.set(name, value);
  }
  return tokens;
}

const themes: Record<Theme, Map<string, string>> = {
  light: tokensOf(":root"),
  dark: new Map([...tokensOf(":root"), ...tokensOf(':root[data-theme="dark"]')]),
};

function resolve(theme: Theme, value: string, depth = 0): string {
  const reference = /^var\((--[\w-]+)\)$/.exec(value.trim());
  if (!reference) return value.trim();
  const next = themes[theme].get(reference[1]);
  if (next === undefined || depth > 8) throw new Error(`${theme}: cannot resolve ${value}`);
  return resolve(theme, next, depth + 1);
}

function parseColor(raw: string): Rgba {
  const value = raw.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(value);
  if (hex) {
    const digits = hex[1].length === 3 ? [...hex[1]].map((digit) => digit + digit).join("") : hex[1];
    return {
      r: parseInt(digits.slice(0, 2), 16),
      g: parseInt(digits.slice(2, 4), 16),
      b: parseInt(digits.slice(4, 6), 16),
      a: 1,
    };
  }
  const functional = /^rgba?\(([^)]+)\)$/.exec(value);
  if (functional) {
    const [r, g, b, a = "1"] = functional[1].split(",").map((part) => part.trim());
    return { r: Number(r), g: Number(g), b: Number(b), a: Number(a) };
  }
  throw new Error(`unsupported colour: ${raw}`);
}

function colorOf(theme: Theme, token: string): Rgba {
  const value = themes[theme].get(token);
  if (value === undefined) throw new Error(`${theme}: ${token} is not defined`);
  return parseColor(resolve(theme, value));
}

/** Source-over compositing of a translucent tint onto an opaque ground. */
function over(foreground: Rgba, ground: Rgba): Rgba {
  const mix = (top: number, bottom: number) => top * foreground.a + bottom * (1 - foreground.a);
  return { r: mix(foreground.r, ground.r), g: mix(foreground.g, ground.g), b: mix(foreground.b, ground.b), a: 1 };
}

/** `color-mix(in srgb, first weight%, second)`: per-channel interpolation of the encoded values. */
function mixSrgb(first: Rgba, weight: number, second: Rgba): Rgba {
  const blend = (a: number, b: number) => a * weight + b * (1 - weight);
  return { r: blend(first.r, second.r), g: blend(first.g, second.g), b: blend(first.b, second.b), a: 1 };
}

function luminance({ r, g, b }: Rgba): number {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(foreground: Rgba, ground: Rgba): number {
  const a = luminance(foreground);
  const b = luminance(ground);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

const WHITE: Rgba = { r: 255, g: 255, b: 255, a: 1 };

/** Every surface body text is set on. --surface-hover is deliberately not here: only --text sits on it. */
const TEXT_GROUNDS = ["--bg", "--sidebar", "--surface", "--surface-raised", "--surface-soft"];
const TEXT_TOKENS = ["--text", "--text-muted", "--text-faint", "--accent", "--success", "--danger", "--warning"];
const SEMANTIC_TINTS: Array<[text: string, tint: string]> = [
  ["--accent", "--accent-soft"],
  ["--success", "--success-soft"],
  ["--danger", "--danger-soft"],
  ["--warning", "--warning-soft"],
];
const CODE_TOKENS = ["--code-keyword", "--code-string", "--code-comment", "--code-number", "--code-function"];

describe.each<Theme>(["light", "dark"])("%s theme colour tokens", (theme) => {
  const ratio = (foreground: string, ground: string) => contrast(colorOf(theme, foreground), colorOf(theme, ground));

  it.each(TEXT_TOKENS.flatMap((token) => TEXT_GROUNDS.map((ground) => [token, ground] as const)))(
    "%s reads at 4.5:1 on %s",
    (token, ground) => {
      expect(ratio(token, ground)).toBeGreaterThanOrEqual(AA_TEXT);
    },
  );

  it("keeps muted text readable on the hover and user-inset grounds too", () => {
    expect(ratio("--text-muted", "--surface-hover")).toBeGreaterThanOrEqual(AA_TEXT);
    expect(ratio("--text-muted", "--user-start")).toBeGreaterThanOrEqual(AA_TEXT);
    expect(ratio("--accent-on-inset", "--user-start")).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it("keeps the hierarchy: faint text is never stronger than muted, which is never stronger than body", () => {
    expect(ratio("--text", "--bg")).toBeGreaterThan(ratio("--text-muted", "--bg"));
    expect(ratio("--text-muted", "--bg")).toBeGreaterThan(ratio("--text-faint", "--bg"));
  });

  it.each(["--accent-fill", "--danger-fill"])("white label on filled %s reads at 4.5:1", (fill) => {
    expect(contrast(WHITE, colorOf(theme, fill))).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each(SEMANTIC_TINTS.flatMap(([text, tint]) => ["--surface-elevated", "--bg"].map((ground) => [text, tint, ground] as const)))(
    "%s reads at 4.5:1 on its %s tint over %s",
    (text, tint, ground) => {
      const tinted = over(colorOf(theme, tint), colorOf(theme, ground));
      expect(contrast(colorOf(theme, text), tinted)).toBeGreaterThanOrEqual(AA_TEXT);
    },
  );

  it.each(CODE_TOKENS)("%s reads at 4.5:1 on the code-block ground", (token) => {
    // .code-block: color-mix(in srgb, var(--bg) 88%, var(--surface))
    const codeGround = mixSrgb(colorOf(theme, "--bg"), 0.88, colorOf(theme, "--surface"));
    expect(contrast(colorOf(theme, token), codeGround)).toBeGreaterThanOrEqual(AA_TEXT);
  });
});

describe("filled controls", () => {
  const declarationFor = (selector: string, property: string) => {
    const rule = rules.find((candidate) => candidate.media === null && selectorsOf(candidate).includes(selector));
    return rule ? declarationsOf(rule).get(property) : undefined;
  };

  it.each([".button-primary", ".composer-primary-control-ready"])("%s is painted with --accent-fill, not the text accent", (selector) => {
    expect(declarationFor(selector, "background")).toBe("var(--accent-fill)");
  });

  it("paints the destructive button with --danger-fill", () => {
    expect(declarationFor(".button-danger", "background")).toBe("var(--danger-fill)");
  });

  it("does not paint the primary button hover with the lighter text accent", () => {
    expect(declarationFor(".button-primary:hover:not(:disabled)", "background")).toBe("var(--accent-fill)");
  });
});

describe("warning token", () => {
  it("is defined in both themes, so no rule falls back to a hard-coded colour", () => {
    for (const theme of ["light", "dark"] as const) {
      expect(themes[theme].has("--warning"), `${theme} --warning`).toBe(true);
      expect(themes[theme].has("--warning-soft"), `${theme} --warning-soft`).toBe(true);
    }
    expect(tokensCss).not.toMatch(/var\(--warning\s*,/);
  });
});

describe("font-size floor", () => {
  const sizeInRem = (value: string): number | null => {
    const match = /^([\d.]+)(rem|px)$/.exec(value.trim());
    if (!match) return null;
    return match[2] === "px" ? Number(match[1]) / 16 : Number(match[1]);
  };

  it("never sets text below 11px", () => {
    const offenders: string[] = [];
    for (const rule of rules) {
      const declarations = declarationsOf(rule);
      const size = declarations.get("font-size");
      const shorthand = declarations.get("font")?.split(/\s+/)[0]?.split("/")[0];
      for (const candidate of [size, shorthand]) {
        if (candidate === undefined) continue;
        const rem = sizeInRem(candidate);
        if (rem !== null && rem < MIN_FONT_SIZE_REM) offenders.push(`${rule.selector} { ${candidate} }`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("keeps --text-2xs itself at the floor", () => {
    expect(sizeInRem(themes.light.get("--text-2xs") ?? "")).toBe(MIN_FONT_SIZE_REM);
  });
});
