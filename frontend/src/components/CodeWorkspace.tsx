import { AlertTriangle, Check, ChevronDown, Code2, LoaderCircle, Play, ShieldCheck, X } from "lucide-react";
import { useId, useRef, useState, type KeyboardEvent, type UIEvent } from "react";
import type { CodeCapabilitiesRequest } from "../../../contracts/cortex-api";

type CodeCapabilities = Required<CodeCapabilitiesRequest>;

export type CodeWorkspaceProps = {
  source: string;
  intent: string;
  capabilities: CodeCapabilities;
  busy: boolean;
  onSourceChange: (source: string) => void;
  onIntentChange: (intent: string) => void;
  onCapabilityChange: (capability: keyof CodeCapabilities, enabled: boolean) => void;
  onRun: () => void;
  onClose: () => void;
};

const MAX_SOURCE_LENGTH = 64 * 1024;
const MAX_VISIBLE_LINE_NUMBERS = 2_000;

export function CodeWorkspace({
  source,
  intent,
  capabilities,
  busy,
  onSourceChange,
  onIntentChange,
  onCapabilityChange,
  onRun,
  onClose,
}: CodeWorkspaceProps) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const lineNumbersRef = useRef<HTMLDivElement>(null);
  const [permissionsOpen, setPermissionsOpen] = useState(false);
  const sourceId = useId();
  const intentId = useId();
  const lineCount = Math.max(1, source.split("\n").length);
  const visibleLineCount = Math.min(lineCount, MAX_VISIBLE_LINE_NUMBERS);
  const selectedCapabilityCount = Object.values(capabilities).filter(Boolean).length;
  const canRun = Boolean(source.trim() && intent.trim()) && !busy;

  const handleEditorKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const editor = event.currentTarget;
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      editor.setRangeText("    ", start, end, "end");
      onSourceChange(editor.value);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (canRun) onRun();
    }
  };

  const handleEditorScroll = (event: UIEvent<HTMLTextAreaElement>) => {
    if (lineNumbersRef.current) lineNumbersRef.current.scrollTop = event.currentTarget.scrollTop;
  };

  return (
    <section className="composer-code-workspace" aria-label="Code workspace">
      <header className="composer-code-header">
        <div className="composer-code-heading">
          <span className="composer-code-mark" aria-hidden="true"><Code2 size={17} /></span>
          <div>
            <span className="composer-code-eyebrow">CODE WORKSPACE</span>
            <div className="composer-code-title-row">
              <h2>Write and run Python</h2>
              <span className="composer-code-draft-state">Draft</span>
            </div>
            <p>Paste a model proposal or write a small script. Review it before a one-time approval.</p>
          </div>
        </div>
        <div className="composer-code-header-actions">
          <span className="composer-code-language">Python</span>
          <button className="icon-button icon-button-small" type="button" aria-label="Close code workspace" title="Back to chat" onClick={onClose}>
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="composer-code-layout">
        <div className="composer-code-editor-column">
          <div className="composer-code-editor-heading">
            <label htmlFor={sourceId}>Python source</label>
            <span>{lineCount} {lineCount === 1 ? "line" : "lines"} <span aria-hidden="true">·</span> {source.length.toLocaleString()} / {MAX_SOURCE_LENGTH.toLocaleString()} chars</span>
          </div>
          <div className="composer-code-editor-shell">
            <div ref={lineNumbersRef} className="composer-code-line-numbers" aria-hidden="true">
              {Array.from({ length: visibleLineCount }, (_, index) => <span key={index}>{index + 1}</span>)}
              {lineCount > MAX_VISIBLE_LINE_NUMBERS && <span>…</span>}
            </div>
            <textarea
              ref={editorRef}
              id={sourceId}
              className="composer-code-editor"
              value={source}
              maxLength={MAX_SOURCE_LENGTH}
              onChange={(event) => onSourceChange(event.target.value)}
              onKeyDown={handleEditorKeyDown}
              onScroll={handleEditorScroll}
              placeholder="# Start writing Python here"
              aria-label="Python source"
              spellCheck={false}
              wrap="off"
            />
            {!source && <div className="composer-code-editor-empty" aria-hidden="true"><Code2 size={18} /><span>Start with a small, explicit script</span></div>}
          </div>
          <div className="composer-code-editor-hint">
            <span><kbd>Ctrl</kbd><span aria-hidden="true">/</span><kbd>⌘</kbd> + <kbd>Enter</kbd> to review</span>
            <span>Python only <span aria-hidden="true">·</span> no code runs in the editor</span>
          </div>
        </div>

        <aside className="composer-code-side" aria-label="Run review">
          <div className="composer-code-request-card">
            <div className="composer-code-section-heading">
              <div><span className="composer-code-step">1</span><span>Describe the run</span></div>
              <span className="composer-code-step-label">Required</span>
            </div>
            <label className="composer-code-intent" htmlFor={intentId}>
              <span>Run intent</span>
              <textarea
                id={intentId}
                value={intent}
                maxLength={500}
                rows={3}
                onChange={(event) => onIntentChange(event.target.value)}
                placeholder="What should this script do?"
                aria-label="Run intent"
              />
              <small>Keep the outcome specific so the approval card is easy to verify.</small>
            </label>
          </div>

          <div className="composer-code-safety-card">
            <div className="composer-code-safety-heading">
              <ShieldCheck size={16} aria-hidden="true" />
              <div><strong>Sandboxed by default</strong><span>No files, processes, or network access.</span></div>
            </div>
            <details open={permissionsOpen} onToggle={(event) => setPermissionsOpen(event.currentTarget.open)} className="composer-code-permissions">
              <summary>
                <span>Permissions</span>
                <span className={selectedCapabilityCount ? "composer-code-permissions-count composer-code-permissions-count-warning" : "composer-code-permissions-count"}>{selectedCapabilityCount ? `${selectedCapabilityCount} enabled` : "Sandboxed"}</span>
                <ChevronDown size={14} aria-hidden="true" />
              </summary>
              <div className="composer-code-permissions-body">
                <p>Only enable access this run needs. Permissions expire when it finishes.</p>
                <div className="composer-code-capabilities" aria-label="Requested capabilities">
                  <CapabilityToggle capability="filesystem" label="Files" description="Read or write local files" enabled={capabilities.filesystem} onChange={(enabled) => onCapabilityChange("filesystem", enabled)} />
                  <CapabilityToggle capability="process" label="Processes" description="Start a local command" enabled={capabilities.process} onChange={(enabled) => onCapabilityChange("process", enabled)} />
                  <CapabilityToggle capability="network" label="Network" description="Make HTTP requests" enabled={capabilities.network} onChange={(enabled) => onCapabilityChange("network", enabled)} />
                </div>
                {selectedCapabilityCount > 0 && (
                  <div className="composer-code-access-warning" role="note">
                    <AlertTriangle size={13} aria-hidden="true" />
                    <span>This run can access your machine. Review each permission before approving.</span>
                  </div>
                )}
              </div>
            </details>
          </div>

          <div className="composer-code-actions">
            <button className="button button-quiet" type="button" onClick={onClose} disabled={busy}>Back to chat</button>
            <button className="button button-primary composer-code-run" type="button" onClick={onRun} disabled={!canRun}>
              {busy ? <LoaderCircle size={14} className="composer-control-spinner" aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
              Review &amp; request
            </button>
          </div>
        </aside>
      </div>

      <footer className="composer-code-footer">
        <span><Check size={13} aria-hidden="true" /> Every run requires approval</span>
        <span>Generated code stays collapsed in task activity until you expand it.</span>
      </footer>
    </section>
  );
}

function CapabilityToggle({
  capability,
  label,
  description,
  enabled,
  onChange,
}: {
  capability: keyof CodeCapabilities;
  label: string;
  description: string;
  enabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <label className={`composer-code-capability${enabled ? " composer-code-capability-active" : ""}`}>
      <input
        type="checkbox"
        checked={enabled}
        aria-label={`${label} capability`}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <strong>{label}</strong>
        <small>{description}</small>
      </span>
      <span className="composer-code-capability-state" aria-hidden="true">{enabled ? "On" : "Off"}</span>
      <span className="sr-only">{capability}</span>
    </label>
  );
}
