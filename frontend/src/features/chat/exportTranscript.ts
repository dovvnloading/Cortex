import type { ChatResponse } from "../../../../contracts/cortex-api";

export function transcriptAsMarkdown(chat: ChatResponse): string {
  const lines = (chat.messages ?? []).map((message) => {
    const label = message.role === "assistant" ? "Cortex" : message.role === "user" ? "You" : "System";
    return `### ${label}\n\n${message.content}`;
  });
  return lines.join("\n\n---\n\n");
}

export function transcriptAsJson(chat: ChatResponse): string {
  return JSON.stringify(chat, null, 2);
}

function sanitizeFilename(name: string): string {
  return name.replace(/[/\\:*?"<>|]/g, "_").trim() || "chat";
}

function downloadBlob(filename: string, content: string, mimeType: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mimeType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function exportTranscriptAsMarkdown(chat: ChatResponse): void {
  downloadBlob(`${sanitizeFilename(chat.title)}.md`, transcriptAsMarkdown(chat), "text/markdown");
}

export function exportTranscriptAsJson(chat: ChatResponse): void {
  downloadBlob(`${sanitizeFilename(chat.title)}.json`, transcriptAsJson(chat), "application/json");
}
