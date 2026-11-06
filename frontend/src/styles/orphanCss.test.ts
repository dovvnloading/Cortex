// @vitest-environment node
import { describe, expect, it } from "vitest";
import { parseRules, selectorsOf, tokensCss } from "../test/css";

/**
 * A stylesheet only ever grows unless something notices a rule that nothing
 * uses any more: a component is deleted or renamed, its CSS stays, and the next
 * reader takes it for live. This reads every class selector in `tokens.css` and
 * fails for one that no component names.
 */

/** Every non-test component and module, as source text. */
const sources = Object.values(
  import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../**/*.test.ts", "!../**/*.test.tsx", "!../test/**"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>,
);

/**
 * Classes that are real but never appear literally in the source, and why.
 * Each pattern must still match something in the stylesheet, so the list cannot
 * quietly outlive the thing it excuses.
 */
const ALLOWED: ReadonlyArray<{ pattern: RegExp; reason: string }> = [
  { pattern: /^hljs-/, reason: "token classes rehype-highlight adds to highlighted code" },
  { pattern: /^(class|function)_$/, reason: "highlight.js sub-scopes, as in .hljs-title.function_" },
  { pattern: /^(contains-task-list|task-list-item)$/, reason: "emitted by remark-gfm for a Markdown task list" },
  { pattern: /^message-(user|assistant|system)$/, reason: "built as message-${role} in MessageCard" },
  { pattern: /^model-picker-status-/, reason: "built from the runtime tone in LocalModelMenu" },
  { pattern: /^runtime-badge-/, reason: "built from the runtime tone in MessageComposer" },
  { pattern: /^toast-(error|info|success)$/, reason: "built as toast-${kind} in ToastProvider" },
];

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Whether a component names the class as a whole word (`chat-row` is not found in `chat-row-active`). */
function isNamedInSource(className: string): boolean {
  const whole = new RegExp(`(^|[^\\w-])${escapeRegExp(className)}(?![\\w-])`);
  return sources.some((text) => whole.test(text));
}

const stylesheetClasses = (() => {
  const names = new Set<string>();
  for (const rule of parseRules(tokensCss)) {
    for (const selector of selectorsOf(rule)) {
      for (const match of selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) names.add(match[1]);
    }
  }
  return [...names].sort();
})();

describe("tokens.css class selectors", () => {
  it("reads the stylesheet and the source it is checked against", () => {
    // Guards the test itself: an empty glob or an unreadable stylesheet would
    // make the check below pass for the wrong reason.
    expect(stylesheetClasses.length).toBeGreaterThan(100);
    expect(sources.length).toBeGreaterThan(40);
    expect(isNamedInSource("code-block")).toBe(true);
    expect(isNamedInSource("definitely-not-a-class-anyone-uses")).toBe(false);
    // Whole words only: a longer class does not vouch for its prefix.
    expect(isNamedInSource("chat-row-selec")).toBe(false);
  });

  it("are each used by a component, or explained", () => {
    const orphans = stylesheetClasses.filter(
      (name) => !isNamedInSource(name) && !ALLOWED.some(({ pattern }) => pattern.test(name)),
    );

    expect(orphans, `no component uses these classes; delete their rules or add them to ALLOWED with a reason:\n${orphans.join("\n")}`).toEqual([]);
  });

  it("keeps every allowed pattern in use", () => {
    const unused = ALLOWED.filter(({ pattern }) => !stylesheetClasses.some((name) => pattern.test(name)));

    expect(unused.map(({ reason }) => reason)).toEqual([]);
  });
});
