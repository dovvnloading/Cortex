import { ArrowUp, FileText, Image as ImageIcon, LoaderCircle, Paperclip, Square, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FocusEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import type { ChatAttachment, GenerationOptionsOverride, GenerationSettings } from "../../../../contracts/cortex-api";
import { isGGUFModel } from "../../lib/localModels";
import { GENERATION_DEFAULTS } from "../../lib/generationParams";
import { useModelStore } from "../../stores/useModelStore";
import { GenerationParamsPopover } from "./GenerationParamsPopover";
import { LocalModelMenu, type ModelRuntimeStatus } from "../models/LocalModelMenu";

/** Only computed for a selected GGUF model -- Ollama has no comparable
 * "is the runtime actually loaded yet" state worth surfacing here. Keeps
 * the user from wondering whether sending a message will trigger a local
 * model load, and shows the currently active state as soon as this
 * component polls it, not only when a generation is already in flight. */
function useLlamaRuntimeStatus(selectedModel: string | null): ModelRuntimeStatus | null {
  const llamacppStatus = useModelStore((state) => state.llamacppStatus);
  if (!llamacppStatus || !selectedModel || !isGGUFModel(selectedModel)) return null;

  const isThisModelLoaded = llamacppStatus.state === "ready" && llamacppStatus.loaded_model === selectedModel;
  const backend = llamacppStatus.active_backend === "vulkan" ? " · GPU" : llamacppStatus.active_backend === "cpu" ? " · CPU" : "";
  // The detail prefers the hard error; otherwise it explains the most recent
  // runtime restart (model change, context increase, crash) -- a model
  // reload costs minutes, so the reason should never be a mystery.
  const detail = llamacppStatus.last_error ?? llamacppStatus.last_restart_reason ?? null;
  if (isThisModelLoaded) return { tone: "ready", label: `Loaded${backend}`, detail };
  if (llamacppStatus.state === "downloading_binary") return { tone: "starting", label: "Downloading runtime…", detail };
  if (llamacppStatus.state === "starting") return { tone: "starting", label: "Starting…", detail };
  if (llamacppStatus.state === "failed") return { tone: "failed", label: "Failed to start", detail };
  return { tone: "idle", label: "Not loaded yet", detail: "Loads when you send a message." };
}

export type ComposerPhase = "ready" | "starting" | "generating" | "stopping" | "finishing" | "unavailable";

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
  generationOptions?: GenerationOptionsOverride | null;
  generationDefaults?: GenerationSettings;
  onGenerationOptionsChange?: (next: GenerationOptionsOverride | null) => void;
};

const MAX_MESSAGE_LENGTH = 100_000;
const MIN_TEXTAREA_HEIGHT = 54;
const MAX_TEXTAREA_HEIGHT = 200;

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
  generationOptions = null,
  generationDefaults = GENERATION_DEFAULTS,
  onGenerationOptionsChange,
}: MessageComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const modelDetails = useModelStore((state) => state.models?.models);
  const runtimeStatus = useLlamaRuntimeStatus(selectedModel);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const submissionPendingRef = useRef(false);
  const stopPendingRef = useRef(false);
  const composingRef = useRef(false);
  const [submissionPending, setSubmissionPending] = useState(false);
  const [focused, setFocused] = useState(false);
  const statusId = useId();
  const counterId = useId();
  const attachmentInputId = useId();
  const canSubmit = phase === "ready"
    && Boolean(value.trim() || attachments.length > 0)
    && !submissionPending
    && !attachmentsBusy
    && !imageInputBlocked;
  const isStopping = phase === "stopping";
  const isFinishing = phase === "finishing";
  const isGenerating = phase === "generating" || isStopping || isFinishing;
  // Runtime availability gates sending, not workspace configuration. Users
  // must still be able to choose a discovered model while the current model
  // is missing or the local service is reconnecting.
  const controlsLocked = phase === "starting" || isGenerating;
  const remaining = MAX_MESSAGE_LENGTH - value.length;
  const showCounter = remaining <= 1_000;

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
    if (!isGenerating || isStopping || isFinishing || stopPendingRef.current) return;
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

  const handleSurfaceBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
  };

  const handleAttachmentChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length && onAddAttachments) void onAddAttachments(files);
  };

  const status = phase === "starting"
    ? "Starting response…"
    : phase === "stopping"
      ? "Stopping response…"
      : phase === "finishing"
        ? "Finishing response…"
      : phase === "generating"
        ? generationElsewhere ? "Generating in another thread" : "Generating…"
        : phase === "unavailable"
          ? runtimeMessage ?? "The local runtime is unavailable."
          : focused
            ? "Enter to send · Shift+Enter for a new line"
            : "";
  // The keyboard hint follows focus, so it appears the moment any toolbar
  // control is pressed. Marked separately so narrow layouts can drop it: there
  // it would claim a row mid-click and shift the control out from under the
  // pointer before the click lands.
  const statusIsKeyboardHint = phase === "ready" && focused;

  return (
    <div className="composer-area">
      {error && (
        <div className="composer-error" role="alert">
          <span className="composer-error-copy">{error}</span>
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

          <div className="composer-writing-area">
            <textarea
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
            />
          </div>

          {(attachmentError || imageInputBlocked) && (
            <div className="composer-inline-alert" role="alert">
              <span>{attachmentError ?? imageInputBlocked}</span>
            </div>
          )}

          <div className="composer-toolbar">
            <div className="composer-toolbar-leading">
              <label className={`composer-attach-control${attachmentsBusy ? " composer-attach-control-busy" : ""}`} htmlFor={attachmentInputId} title="Attach images or documents">
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
              <div className="composer-model-control">
                <LocalModelMenu
                  models={localModels}
                  details={modelDetails}
                  selectedModel={selectedModel}
                  onSelect={onSelectModel}
                  onRescan={onRescanModels}
                  disabled={controlsLocked || modelBusy}
                  runtimeStatus={runtimeStatus}
                />
                {/* Quiet states live in the picker's dot and tooltip; only a
                    runtime that is busy or broken earns words in the toolbar. */}
                {(runtimeStatus?.tone === "starting" || runtimeStatus?.tone === "failed") && (
                  <span className={`runtime-badge runtime-badge-${runtimeStatus.tone}`} title={runtimeStatus.detail ?? undefined}>
                    {runtimeStatus.label}
                  </span>
                )}
              </div>
              {onGenerationOptionsChange && (
                <GenerationParamsPopover
                  value={generationOptions}
                  defaults={generationDefaults}
                  disabled={controlsLocked}
                  onChange={onGenerationOptionsChange}
                />
              )}
            </div>

            <div className="composer-toolbar-trailing">
              <span id={statusId} className={`composer-status${status ? " composer-status-visible" : ""}${statusIsKeyboardHint ? " composer-status-hint" : ""}`} role="status" aria-live="polite" aria-atomic="true" title={status || undefined}>
                {status}
              </span>
              {showCounter && <span id={counterId} className="composer-counter">{remaining.toLocaleString()} left</span>}
              {isGenerating ? (
                <button
                  className="composer-primary-control composer-stop-control"
                  type="button"
                  aria-label={isStopping ? "Stopping response" : isFinishing ? "Finishing response" : "Stop generating"}
                  title={isStopping ? "Stopping response" : isFinishing ? "Finishing response" : "Stop generating"}
                  disabled={isStopping || isFinishing}
                  onClick={() => void stop()}
                >
                  {isStopping || isFinishing ? <LoaderCircle aria-hidden="true" size={17} className="composer-control-spinner" /> : <Square aria-hidden="true" size={15} fill="currentColor" />}
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
          </div>
        </div>
      </form>
    </div>
  );
}
