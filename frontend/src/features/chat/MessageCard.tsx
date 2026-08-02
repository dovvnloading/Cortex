import { useState } from "react";
import { Copy, FileText, GitBranch, Image as ImageIcon, RefreshCw } from "lucide-react";
import type { ChatAttachment, ChatMessage, GenerationStats } from "../../../../contracts/cortex-api";
import { MessageStats } from "./MessageStats";
import { SafeMarkdown } from "../markdown/SafeMarkdown";

function AttachmentList({ attachments }: { attachments?: ChatAttachment[] | null }) {
  if (!attachments?.length) return null;
  return (
    <div className="message-attachments" aria-label="Attached files">
      {attachments.map((attachment) => (
        <span className="message-attachment" key={attachment.attachment_id} title={attachment.filename}>
          {attachment.kind === "image" ? <ImageIcon size={14} aria-hidden="true" /> : <FileText size={14} aria-hidden="true" />}
          <span className="message-attachment-name">{attachment.filename}</span>
        </span>
      ))}
    </div>
  );
}

export function MessageCard({ message, isFinalAssistant, busy, onRegenerate, onFork, forking }: { message: ChatMessage; isFinalAssistant: boolean; busy: boolean; onRegenerate: () => void; onFork: () => void; forking: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <article className={`message-card message-${message.role}`} aria-label={`${message.role === "assistant" ? "Cortex" : message.role === "user" ? "Your" : "System"} message`}>
      <MessageIdentity role={message.role} timestamp={message.timestamp} stats={message.stats} />
      <div className="message-bubble">
        <AttachmentList attachments={message.attachments} />
        {message.role === "assistant" && message.thoughts && <details className="reasoning"><summary><span>Reasoning</span><span className="disclosure-hint">Show details</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.thoughts} /></div></div></details>}
        <div className="markdown-body">{message.role === "user" ? <p>{message.content}</p> : <SafeMarkdown content={message.content} />}</div>
        {message.sources && message.sources.length > 0 && <details className="sources"><summary><span>Sources</span><span className="disclosure-hint">{message.sources.length} {message.sources.length === 1 ? "item" : "items"}</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.sources.map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("\n\n")} /></div></div></details>}
      </div>
      <div className="message-actions" aria-label="Message actions">
        <button className="icon-button icon-button-small" type="button" aria-label={copied ? "Message copied" : "Copy message"} title={copied ? "Copied" : "Copy message"} onClick={() => void copy()}><Copy size={14} aria-hidden="true" />{copied && <span className="message-action-feedback">Copied</span>}</button>
        {message.role === "assistant" && <>
          <button className="icon-button icon-button-small" type="button" aria-label="Regenerate response" title="Regenerate response" disabled={!isFinalAssistant || busy} onClick={onRegenerate}><RefreshCw size={14} aria-hidden="true" /></button>
          <button className="icon-button icon-button-small" type="button" aria-label="Fork chat from this message" title="Fork chat from this message" disabled={busy || forking || !message.id} onClick={onFork}><GitBranch size={14} aria-hidden="true" /></button>
        </>}
      </div>
    </article>
  );
}

export function MessageIdentity({ role, timestamp, stats }: { role: ChatMessage["role"]; timestamp?: string | null; stats?: GenerationStats | null }) {
  const label = role === "assistant" ? "Cortex" : role === "user" ? "You" : "System";
  const mark = role === "assistant" ? "C" : role === "user" ? "Y" : "S";
  const displayTime = formatMessageTime(timestamp);
  return <div className="message-meta"><span className={`message-author-mark message-author-mark-${role}`} aria-hidden="true">{mark}</span><span className="message-author">{label}</span>{displayTime && <time dateTime={timestamp ?? undefined}>{displayTime}</time>}<MessageStats stats={stats} /></div>;
}

function formatMessageTime(value?: string | null): string | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return null;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(timestamp);
}
