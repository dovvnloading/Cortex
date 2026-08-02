import { isValidElement, useState, type ComponentProps, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
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

function Link({ href, children, ...props }: ComponentProps<"a">) {
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

function Code({ className, children, ...props }: ComponentProps<"code">) {
  const value = childrenToText(children).replace(/\n$/, "");
  const fenced = Boolean(className) || value.includes("\n");
  if (!fenced) return <code className={className} {...props}>{children}</code>;

  const language = languageLabel(className);
  return (
    <span className="code-block-content">
      <span className="code-block-toolbar">
        <span className="code-language">{language}</span>
        <CodeCopyButton value={value} language={language} />
      </span>
      <code className={className} {...props}>{children}</code>
    </span>
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

function Table({ children, ...props }: ComponentProps<"table">) {
  return (
    <div className="markdown-table-wrap">
      <table {...props}>{children}</table>
    </div>
  );
}

const components = {
  a: Link,
  code: Code,
  img: () => null,
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

export function SafeMarkdown({ content, finalized = true }: SafeMarkdownProps) {
  // Sanitize first, then highlight: rehype-highlight only adds classNames to
  // an already-safe tree, so there is nothing left for sanitize to strip.
  const rehypePlugins = finalized ? [rehypeSanitize, rehypeHighlight] : [rehypeSanitize];
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={rehypePlugins} components={components}>
      {content}
    </ReactMarkdown>
  );
}
