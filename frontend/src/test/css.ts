import tokensSource from "../styles/tokens.css?raw";

/**
 * A minimal reader for `styles/tokens.css`, for tests that need to assert on
 * the stylesheet itself (colour contrast, font-size floor, forced-colors
 * coverage). jsdom does not evaluate media queries or compute custom
 * properties, so the source text is the only thing a unit test can inspect.
 */

export type CssRule = {
  /** The selector list exactly as written, trimmed. */
  selector: string;
  body: string;
  /** The enclosing `@media` query, or null for a top-level rule. */
  media: string | null;
};

export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** The stylesheet with comments removed, so a `;` or `{` inside prose cannot confuse the parsers below. */
export const tokensCss: string = stripComments(tokensSource);

/** Flatten rules, descending into `@media` and skipping other at-rules such as `@keyframes`. */
export function parseRules(text: string, media: string | null = null): CssRule[] {
  const rules: CssRule[] = [];
  let index = 0;
  while (index < text.length) {
    const open = text.indexOf("{", index);
    if (open === -1) break;
    const prelude = text.slice(index, open).trim();
    let depth = 1;
    let cursor = open + 1;
    while (cursor < text.length && depth > 0) {
      if (text[cursor] === "{") depth += 1;
      else if (text[cursor] === "}") depth -= 1;
      cursor += 1;
    }
    const body = text.slice(open + 1, cursor - 1);
    if (prelude.startsWith("@media")) rules.push(...parseRules(body, prelude.slice("@media".length).trim()));
    else if (!prelude.startsWith("@")) rules.push({ selector: prelude, body, media });
    index = cursor;
  }
  return rules;
}

export function selectorsOf(rule: CssRule): string[] {
  return rule.selector.split(",").map((part) => part.trim()).filter(Boolean);
}

export function declarationsOf(rule: CssRule): Map<string, string> {
  const declarations = new Map<string, string>();
  for (const match of rule.body.matchAll(/([\w-]+)\s*:\s*([^;]+);?/g)) {
    declarations.set(match[1], match[2].trim());
  }
  return declarations;
}
