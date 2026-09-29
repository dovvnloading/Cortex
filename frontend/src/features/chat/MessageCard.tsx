import { memo, useState } from "react";
import { Copy, FileText, GitBranch, Image as ImageIcon, RefreshCw } from "lucide-react";
import type { ChatAttachment, ChatMessage, GenerationStats } from "../../../../contracts/cortex-api";
import { formatMessageTime } from "../../lib/messageTime";
import { useChatStore } from "../../stores/useChatStore";
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

type MessageCardProps = {
  message: ChatMessage;
  /** Position in the transcript. Handed back to `onRegenerate` so one callback can serve every card. */
  index: number;
  isFinalAssistant: boolean;
  busy: boolean;
  forking: boolean;
  /**
   * Both callbacks take the message rather than being bound to it, so the
   * parent can pass one stable function to every card. A closure per card, new
   * on every render, defeated `memo` below and re-parsed every message's
   * Markdown for each streamed frame.
   */
  onRegenerate: (message: ChatMessage, index: number) => void;
  onFork: (message: ChatMessage) => void;
};

/**
 * Memoised: a transcript card only has to render again when its own message or
 * its own busy/final/forking state changes, not whenever the transcript around
 * it does. That is what keeps a long chat cheap while a reply streams in.
 */
export const MessageCard = memo(function MessageCard({ message, index, isFinalAssistant, busy, forking, onRegenerate, onFork }: MessageCardProps) {
  const [copied, setCopied] = useState(false);
  const untranslated = useChatStore((state) => (message.id ? state.untranslatedMessageIds[message.id] === true : false));
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
      <div className="message-bubble">
        <AttachmentList attachments={message.attachments} />
        {message.role === "assistant" && !message.content && message.thoughts ? (
          <p className="muted-note">Cortex ran out of room to finish this answer -- see its reasoning below.</p>
        ) : (
          <div className="markdown-body">{message.role === "user" ? <p>{message.content}</p> : <SafeMarkdown content={message.content} />}</div>
        )}
        {message.role === "assistant" && untranslated && <p className="muted-note" role="note">Couldn't translate this answer; showing the original.</p>}
        {message.sources && message.sources.length > 0 && <details className="sources"><summary><span>Sources</span><span className="disclosure-hint">{message.sources.length} {message.sources.length === 1 ? "item" : "items"}</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.sources.map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("\n\n")} /></div></div></details>}
      </div>
      {message.role === "assistant" && message.thoughts && <details className="reasoning"><summary><span>Reasoning</span><span className="disclosure-hint">Show details</span></summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={message.thoughts} /></div></div></details>}
      {/* Metadata sits below the message and stays quiet. The speaker is
          already unambiguous from the form -- unframed text on the left is
          Cortex, an inset on the right is you -- so repeating a nameplate
          above every turn was chrome carrying no information. Screen readers
          still get the speaker from this article's aria-label. */}
      <div className="message-footer">
        <div className="message-actions" aria-label="Message actions">
          <button className="icon-button icon-button-small" type="button" aria-label={copied ? "Message copied" : "Copy message"} title={copied ? "Copied" : "Copy message"} onClick={() => void copy()}><Copy size={14} aria-hidden="true" />{copied && <span className="message-action-feedback">Copied</span>}</button>
          {message.role === "assistant" && <>
            <button className="icon-button icon-button-small" type="button" aria-label="Regenerate response" title="Regenerate response" disabled={!isFinalAssistant || busy} onClick={() => onRegenerate(message, index)}><RefreshCw size={14} aria-hidden="true" /></button>
            <button className="icon-button icon-button-small" type="button" aria-label="Fork chat from this message" title="Fork chat from this message" disabled={busy || forking || !message.id} onClick={() => onFork(message)}><GitBranch size={14} aria-hidden="true" /></button>
          </>}
        </div>
        <MessageMeta timestamp={message.timestamp} stats={message.stats} />
      </div>
    </article>
  );
});

function MessageMeta({ timestamp, stats }: { timestamp?: string | null; stats?: GenerationStats | null }) {
  const displayTime = formatMessageTime(timestamp);
  if (!displayTime && !stats?.tokens_per_second && !stats?.stopped) return null;
  return <div className="message-meta">{displayTime && <time dateTime={timestamp ?? undefined}>{displayTime}</time>}<MessageStats stats={stats} /></div>;
}
