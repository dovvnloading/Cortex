import { AlertTriangle, ArrowUp, Code2, FileText, Image as ImageIcon, LoaderCircle, Paperclip, Play, Square, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { LocalModelMenu } from "./LocalModelMenu";
import type { ChatAttachment, CodeCapabilitiesRequest, CodeExecutionRequest } from "../../../contracts/cortex-api";

type CodeCapabilities = Required<CodeCapabilitiesRequest>;

export type ComposerPhase = "ready" | "starting" | "generating" | "stopping" | "unavailable";

export type MessageComposerProps = {
  value: string;
  phase: ComposerPhase;
  selectedModel: string | null;
  localModels: readonly string[];
  runtimeMessage?: string | null;
  generationElsewhere?: boolean;
  modelBusy?: boolean;
  error?: string | null;
  onValueChange: (value: string) => void;
  /** Resolves after Cortex has accepted the request, not merely after a click. */
  onSubmit: () => Promise<void | boolean>;
  onStop: () => Promise<void> | void;
  onSelectModel: (model: string) => Promise<void | boolean>;
  onRescanModels?: () => Promise<void> | void;
  onRetry?: () => Promise<void | boolean> | void | boolean;
  onDismissError?: () => void;
  attachments?: readonly ChatAttachment[];
  attachmentsBusy?: boolean;
  attachmentError?: string | null;
  imageInputBlocked?: string | null;
  onAddAttachments?: (files: File[]) => Promise<void> | void;
  onRemoveAttachment?: (attachmentId: string) => void;
  codeExecutionAvailable?: boolean;
  onRunCode?: (payload: CodeExecutionRequest) => Promise<void>;
};

const MAX_MESSAGE_LENGTH = 100_000;
const MIN_TEXTAREA_HEIGHT = 52;
const MAX_TEXTAREA_HEIGHT = 188;

export function MessageComposer({
  value,
  phase,
  selectedModel,
  localModels,
  runtimeMessage,
  generationElsewhere = false,
  modelBusy = false,
  error,
  onValueChange,
  onSubmit,
  onStop,
  onSelectModel,
  onRescanModels,
  onRetry,
  onDismissError,
  attachments = [],
  attachmentsBusy = false,
  attachmentError = null,
  imageInputBlocked = null,
  onAddAttachments,
  onRemoveAttachment,
  codeExecutionAvailable = false,
  onRunCode,
}: MessageComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const submissionPendingRef = useRef(false);
  const stopPendingRef = useRef(false);
  const composingRef = useRef(false);
  const [submissionPending, setSubmissionPending] = useState(false);
  const [focused, setFocused] = useState(false);
  const [codePanelOpen, setCodePanelOpen] = useState(false);
  const [codeSource, setCodeSource] = useState("print('Hello from Cortex')\n");
  const [codeIntent, setCodeIntent] = useState("Run this local Python task");
  const [codeCapabilities, setCodeCapabilities] = useState<CodeCapabilities>({ filesystem: false, process: false, network: false });
  const [codeBusy, setCodeBusy] = useState(false);
  const selectedCodeCapabilityCount = Object.values(codeCapabilities).filter(Boolean).length;
  const statusId = useId();
  const counterId = useId();
  const canSubmit = phase === "ready"
    && Boolean(value.trim() || attachments.length > 0)
    && !submissionPending
    && !attachmentsBusy
    && !imageInputBlocked;
  const isStopping = phase === "stopping";
  const isGenerating = phase === "generating" || isStopping;
  const remaining = MAX_MESSAGE_LENGTH - value.length;
  const showCounter = remaining <= 1_000;
  const attachmentInputId = useId();

  const resizeTextarea = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, MIN_TEXTAREA_HEIGHT), MAX_TEXTAREA_HEIGHT);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    resizeTextarea();
  }, [resizeTextarea, value]);

  useEffect(() => {
    const node = surfaceRef.current;
    if (!node || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => resizeTextarea());
    observer.observe(node);
    return () => observer.disconnect();
  }, [resizeTextarea]);

  const returnFocus = () => {
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const submit = async () => {
    if (!canSubmit || submissionPendingRef.current) return;
    submissionPendingRef.current = true;
    setSubmissionPending(true);
    try {
      await onSubmit();
    } catch {
      // The page owns request errors so they can be shown next to the draft.
    } finally {
      submissionPendingRef.current = false;
      setSubmissionPending(false);
      returnFocus();
    }
  };

  const stop = async () => {
    if (!isGenerating || isStopping || stopPendingRef.current) return;
    stopPendingRef.current = true;
    try {
      await onStop();
    } finally {
      stopPendingRef.current = false;
      returnFocus();
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (composingRef.current || event.nativeEvent.isComposing) return;

    if (event.key === "Escape" && isGenerating) {
      event.preventDefault();
      void stop();
      return;
    }

    const alternateSubmit = (event.ctrlKey || event.metaKey) && event.key === "Enter";
    const plainSubmit = event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey;
    if ((alternateSubmit || plainSubmit) && phase === "ready") {
      event.preventDefault();
      void submit();
    }
  };

  const handleSurfaceBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
  };

  const handleAttachmentChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length && onAddAttachments) void onAddAttachments(files);
  };

  const runCode = async () => {
    if (!onRunCode || !codeSource.trim() || !codeIntent.trim() || codeBusy) return;
    setCodeBusy(true);
    try {
      const request: CodeExecutionRequest = {
        request_id: typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
        language: "python",
        source: codeSource,
        intent_summary: codeIntent.trim(),
        capabilities: codeCapabilities,
      };
      await onRunCode(request);
      setCodePanelOpen(false);
    } catch {
      // The workspace callback owns the user-visible error message.
    } finally {
      setCodeBusy(false);
    }
  };

  const status = phase === "starting"
    ? "Starting response"
    : phase === "stopping"
      ? "Stopping response"
      : phase === "generating"
        ? generationElsewhere ? "Generating in another conversation" : "Generating"
        : phase === "unavailable"
          ? runtimeMessage ?? "The local runtime is unavailable."
          : focused
            ? "Enter sends · Shift+Enter adds a line"
            : "";

  return (
    <div className="composer-area">
      {error && (
        <div className="composer-error" role="alert">
          <span>{error}</span>
          <span className="composer-error-actions">
            {onRetry && <button className="button button-quiet" type="button" onClick={() => { void Promise.resolve(onRetry()).finally(returnFocus); }}>Retry last message</button>}
            {onDismissError && <button className="button button-quiet" type="button" onClick={onDismissError}>Dismiss</button>}
          </span>
        </div>
      )}
      <form className="composer" onSubmit={handleSubmit}>
        <div
          ref={surfaceRef}
          className={`composer-surface composer-phase-${phase}`}
          onFocus={() => setFocused(true)}
          onBlur={handleSurfaceBlur}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) textareaRef.current?.focus();
          }}
        >
          <label className="sr-only" htmlFor="chat-composer">Message Cortex</label>
          {attachments.length > 0 && (
            <div className="composer-attachments" aria-label="Attached files">
              {attachments.map((attachment) => (
                <div className="composer-attachment" key={attachment.attachment_id}>
                  {attachment.kind === "image" ? <ImageIcon size={14} aria-hidden="true" /> : <FileText size={14} aria-hidden="true" />}
                  <span className="composer-attachment-name" title={attachment.filename}>{attachment.filename}</span>
                  <button
                    className="composer-attachment-remove"
                    type="button"
                    aria-label={`Remove ${attachment.filename}`}
                    title={`Remove ${attachment.filename}`}
                    onClick={() => onRemoveAttachment?.(attachment.attachment_id)}
                    disabled={attachmentsBusy}
                  >
                    <X size={13} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {codePanelOpen && (
            <section className="composer-code-panel" aria-label="Local code task">
              <div className="composer-code-panel-heading">
                <div>
                  <span className="composer-code-eyebrow">LOCAL EXECUTION</span>
                  <strong>Review a Python task</strong>
                  <span>Nothing runs until you approve it.</span>
                </div>
                <span className="composer-code-language">Python</span>
                <button className="icon-button icon-button-small" type="button" aria-label="Close code panel" onClick={() => setCodePanelOpen(false)}><X size={14} aria-hidden="true" /></button>
              </div>
              <label className="composer-code-field" htmlFor="code-intent">
                <span>Task summary</span>
                <input id="code-intent" value={codeIntent} maxLength={500} onChange={(event) => setCodeIntent(event.target.value)} placeholder="What should this task do?" />
              </label>
              <label className="composer-code-field composer-code-source-field" htmlFor="code-source">
                <span>Python source</span>
                <textarea id="code-source" className="composer-code-editor" value={codeSource} maxLength={64 * 1024} onChange={(event) => setCodeSource(event.target.value)} spellCheck={false} />
              </label>
              <div className="composer-code-access">
                <div className="composer-code-access-heading">
                  <div><strong>Host access</strong><span>Off by default. Grant only what this task needs.</span></div>
                  <span className="composer-code-access-count">{selectedCodeCapabilityCount ? `${selectedCodeCapabilityCount} selected` : "No access"}</span>
                </div>
                <div className="composer-code-capabilities" aria-label="Requested capabilities">
                  <CapabilityToggle capability="filesystem" label="Files" description="Read or write local files" enabled={codeCapabilities.filesystem} onChange={(enabled) => setCodeCapabilities((current) => ({ ...current, filesystem: enabled }))} />
                  <CapabilityToggle capability="process" label="Processes" description="Start a local command" enabled={codeCapabilities.process} onChange={(enabled) => setCodeCapabilities((current) => ({ ...current, process: enabled }))} />
                  <CapabilityToggle capability="network" label="Network" description="Make HTTP requests" enabled={codeCapabilities.network} onChange={(enabled) => setCodeCapabilities((current) => ({ ...current, network: enabled }))} />
                </div>
                {selectedCodeCapabilityCount > 0 && (
                  <div className="composer-code-access-warning" role="note">
                    <AlertTriangle size={13} aria-hidden="true" />
                    <span>This run can access your machine. Grant only the permissions it needs.</span>
                  </div>
                )}
              </div>
              <div className="composer-code-footer">
                <span>Review the exact source again on the approval card.</span>
                <div>
                  <button className="button button-quiet" type="button" onClick={() => setCodePanelOpen(false)} disabled={codeBusy}>Cancel</button>
                  <button className="button button-primary composer-code-run" type="button" onClick={() => void runCode()} disabled={codeBusy || !codeSource.trim() || !codeIntent.trim()}>
                    {codeBusy ? <LoaderCircle size={14} className="composer-control-spinner" aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                    Request approval
                  </button>
                </div>
              </div>
            </section>
          )}
          {!codePanelOpen && <textarea
            ref={textareaRef}
            id="chat-composer"
            value={value}
            rows={1}
            maxLength={MAX_MESSAGE_LENGTH}
            enterKeyHint="send"
            aria-describedby={`${statusId}${showCounter ? ` ${counterId}` : ""}`}
            placeholder={phase === "unavailable" ? "Write a message while the local runtime reconnects" : "Message Cortex"}
            onChange={(event) => onValueChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => { composingRef.current = true; }}
            onCompositionEnd={() => { composingRef.current = false; }}
          />}

          <div className="composer-utility-row">
            <label className={`composer-attachment-button${attachmentsBusy ? " composer-attachment-button-busy" : ""}`} htmlFor={attachmentInputId} title="Attach images or documents">
              {attachmentsBusy ? <LoaderCircle size={15} className="composer-control-spinner" aria-hidden="true" /> : <Paperclip size={15} aria-hidden="true" />}
              <span className="sr-only">Attach images or documents</span>
              <input
                id={attachmentInputId}
                className="sr-only"
                type="file"
                multiple
                accept="image/*,text/*,.md,.markdown,.rst,.adoc,.org,.text,.csv,.tsv,.json,.jsonl,.ndjson,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.editorconfig,.log,.lock,.diff,.patch,.rtf,.proto,.graphql,.gql,.tf,.hcl,.srt,.vtt,.plist,.xhtml,.map,.py,.js,.jsx,.ts,.tsx,.java,.c,.cc,.cpp,.h,.hpp,.cs,.go,.rs,.rb,.php,.sql,.sh,.bash,.bat,.ps1,.html,.xml,.css,.scss,.less,.vue,.swift,.kt,.kts,.tex,.ipynb"
                onChange={handleAttachmentChange}
                disabled={attachmentsBusy || !onAddAttachments}
              />
            </label>
            {codeExecutionAvailable && onRunCode && <button className={`composer-attachment-button${codePanelOpen ? " composer-attachment-button-active" : ""}`} type="button" title="Prepare a local code task" aria-label="Prepare a local code task" onClick={() => setCodePanelOpen((open) => !open)} disabled={phase !== "ready" || modelBusy}><Code2 size={15} aria-hidden="true" /><span className="sr-only">Code</span></button>}
            <div className="composer-model-control">
              <LocalModelMenu
                models={localModels}
                selectedModel={selectedModel}
                onSelect={onSelectModel}
                onRescan={onRescanModels}
                disabled={phase !== "ready" || modelBusy}
              />
            </div>
            <span className="composer-meta">
              <span id={statusId} className={`composer-status${status ? " composer-status-visible" : ""}`} role="status" aria-live="polite" aria-atomic="true">
                {status}
              </span>
              {showCounter && <span id={counterId} className="composer-counter">{remaining.toLocaleString()} characters left</span>}
            </span>
          </div>

          {(attachmentError || imageInputBlocked) && (
            <div className="composer-attachment-message" role="alert">
              {attachmentError ?? imageInputBlocked}
            </div>
          )}

          {isGenerating ? (
            <button
              className="composer-primary-control composer-stop-control"
              type="button"
              aria-label={isStopping ? "Stopping response" : "Stop generating"}
              title={isStopping ? "Stopping response" : "Stop generating"}
              disabled={isStopping}
              onClick={() => void stop()}
            >
              {isStopping ? <LoaderCircle aria-hidden="true" size={17} className="composer-control-spinner" /> : <Square aria-hidden="true" size={15} fill="currentColor" />}
            </button>
          ) : (
            <button
              className={`composer-primary-control${canSubmit ? " composer-primary-control-ready" : ""}`}
              type="submit"
              aria-label={phase === "starting" ? "Starting response" : "Send message"}
              title={phase === "starting" ? "Starting response" : "Send message"}
              disabled={!canSubmit}
            >
              {phase === "starting" ? <LoaderCircle aria-hidden="true" size={18} className="composer-control-spinner" /> : <ArrowUp aria-hidden="true" size={19} strokeWidth={2.25} />}
            </button>
          )}
        </div>
      </form>
    </div>
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
