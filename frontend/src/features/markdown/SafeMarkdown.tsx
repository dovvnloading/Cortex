import { isValidElement, memo, useState, useSyncExternalStore, type ComponentProps, type ReactNode } from "react";
import ReactMarkdown, { type ExtraProps } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

function safeHref(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

// react-markdown calls every custom renderer with an extra `node` prop (the
// hast AST node) alongside the normal DOM props, described by `ExtraProps`.
// It must be destructured out here -- left inside `...props`, it would be
// spread onto the real DOM element below and show up as a stray
// node="[object Object]" DOM attribute on every rendered link.
function Link({ href, children, node: _node, ...props }: ComponentProps<"a"> & ExtraProps) {
  void _node;
  const safe = safeHref(href);
  if (!safe) return <span>{children}</span>;
  return <a {...props} href={safe} target="_blank" rel="noopener noreferrer">{children}</a>;
}

/** Flattens react-markdown/rehype-highlight children (plain text, or a tree of highlight spans) back to source text. */
function childrenToText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(childrenToText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return childrenToText(node.props.children);
  return "";
}

function languageLabel(className: string | undefined): string {
  const languageClass = className?.split(/\s+/).find((token) => token.startsWith("language-"));
  return languageClass?.replace(/^language-/, "") || "code";
}

const CODE_WRAP_STORAGE_KEY = "cortex.codeWrap";

/**
 * Whether fenced code wraps instead of scrolling sideways. One preference for
 * every block on the page, remembered across launches: a person who wants long
 * lines wrapped wants it everywhere, not per block. Storage can be blocked or
 * absent (private windows, tests), so the choice then simply lasts until the
 * page is closed.
 */
const codeWrapPreference = (() => {
  // Only set while storage refuses the write; otherwise storage is the truth.
  let sessionOnly: boolean | null = null;
  const listeners = new Set<() => void>();
  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    get(): boolean {
      if (sessionOnly !== null) return sessionOnly;
      try {
        return window.localStorage.getItem(CODE_WRAP_STORAGE_KEY) === "1";
      } catch {
        return false;
      }
    },
    set(next: boolean) {
      try {
        window.localStorage.setItem(CODE_WRAP_STORAGE_KEY, next ? "1" : "0");
        sessionOnly = null;
      } catch {
        sessionOnly = next;
      }
      for (const listener of listeners) listener();
    },
  };
})();

function useCodeWrap(): [boolean, (next: boolean) => void] {
  const wrap = useSyncExternalStore(codeWrapPreference.subscribe, codeWrapPreference.get, () => false);
  return [wrap, codeWrapPreference.set];
}

/**
 * Wraps the whole fenced block so the toolbar is a *sibling* of the scrolling
 * <pre>, not a child of it. Nested inside, the toolbar inherits the code's
 * max-content width, which pushes the right-aligned Copy button off-screen on
 * any block wider than the column -- reachable only by scrolling the code all
 * the way right. As a sibling it stays pinned while only the code scrolls.
 */
function Pre({ children }: ComponentProps<"pre">) {
  const [wrap, setWrap] = useCodeWrap();
  const code = isValidElement<{ className?: string; children?: ReactNode }>(children) ? children : null;
  if (!code) return <pre>{children}</pre>;
  const language = languageLabel(code.props.className);
  const value = childrenToText(code.props.children).replace(/\n$/, "");
  return (
    <div className={wrap ? "code-block code-block-wrap" : "code-block"}>
      <div className="code-block-toolbar">
        <span className="code-language">{language}</span>
        <span className="code-block-actions">
          <button className="code-copy" type="button" aria-pressed={wrap} aria-label={`Wrap ${language} code lines`} onClick={() => setWrap(!wrap)}>Wrap</button>
          <CodeCopyButton value={value} language={language} />
        </span>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

function CodeCopyButton({ value, language }: { value: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };
  return <button className="code-copy" type="button" aria-label={`Copy ${language} code`} onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>;
}

// See the comment on `Link` above: `node` must not reach the rest spread.
function Table({ children, node: _node, ...props }: ComponentProps<"table"> & ExtraProps) {
  void _node;
  return (
    <div className="markdown-table-wrap">
      <table {...props}>{children}</table>
    </div>
  );
}

/**
 * Images are never loaded: a remote image would tell whoever hosts it that this
 * answer was read, which a local-first app must not do on the model's say-so.
 * They are not dropped without a trace either -- what the author meant to show
 * is named in place, and the address is on hover for anyone who wants to open
 * it deliberately. `node` is kept out of the DOM, as for `Link`.
 */
function Image({ alt, src, node: _node }: ComponentProps<"img"> & ExtraProps) {
  void _node;
  const description = alt?.trim();
  const source = typeof src === "string" ? src : "";
  return (
    <span className="markdown-image-placeholder" title={source ? `Image not loaded: ${source}` : "Image not loaded"}>
      {description ? `Image: ${description}` : "Image"}
    </span>
  );
}

const components = {
  a: Link,
  pre: Pre,
  img: Image,
  table: Table,
};

type SafeMarkdownProps = {
  content: string;
  /**
   * Skip syntax highlighting while a message is still streaming, so a fast
   * token stream doesn't re-tokenize/re-highlight a growing code block on
   * every delta. Persisted messages (the default) render fully highlighted.
   */
  finalized?: boolean;
};

// Sanitize first, then highlight: rehype-highlight only adds classNames to an
// already-safe tree, so there is nothing left for sanitize to strip.
const REMARK_PLUGINS = [remarkGfm];
const FINAL_REHYPE_PLUGINS = [rehypeSanitize, rehypeHighlight];
const STREAMING_REHYPE_PLUGINS = [rehypeSanitize];

/**
 * Memoised on `content` and `finalized`, both primitives. Every render of
 * react-markdown re-runs the whole unified pipeline (parse, transform, render),
 * so an unchanged message must not be allowed to render at all -- the parent
 * re-renders far more often than any one message's text changes.
 */
export const SafeMarkdown = memo(function SafeMarkdown({ content, finalized = true }: SafeMarkdownProps) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={finalized ? FINAL_REHYPE_PLUGINS : STREAMING_REHYPE_PLUGINS}
      components={components}
    >
      {content}
    </ReactMarkdown>
  );
});
