import { FileJson, FileText } from "lucide-react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import { exportTranscriptAsJson, exportTranscriptAsMarkdown } from "./exportTranscript";

export function ExportTranscriptMenu({ chat }: { chat: ChatResponse }) {
  return (
    <div className="export-transcript-menu" role="group" aria-label="Export this chat">
      <button
        className="icon-button icon-button-small"
        type="button"
        aria-label="Export as Markdown"
        title="Export as Markdown"
        onClick={() => exportTranscriptAsMarkdown(chat)}
      >
        <FileText size={15} aria-hidden="true" />
      </button>
      <button
        className="icon-button icon-button-small"
        type="button"
        aria-label="Export as JSON"
        title="Export as JSON"
        onClick={() => exportTranscriptAsJson(chat)}
      >
        <FileJson size={15} aria-hidden="true" />
      </button>
    </div>
  );
}
