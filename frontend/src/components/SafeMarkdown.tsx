import { useState, type ComponentProps } from "react";
import ReactMarkdown from "react-markdown";
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

function Code({ className, children, ...props }: ComponentProps<"code">) {
  const [copied, setCopied] = useState(false);
  const value = String(children).replace(/\n$/, "");
  const copy = async () => {
    if (!navigator.clipboard) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  const fenced = Boolean(className);
  return (
    <code className={className} {...props}>
      {children}
      {fenced && <button className="code-copy" type="button" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>}
    </code>
  );
}

export function SafeMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      components={{
        a: Link,
        code: Code,
        img: () => null,
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
