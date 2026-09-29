import { RotateCcw, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type {
  CortexSettings,
  LlamaCppRuntimeStatus,
  ModelDownloadRequest,
  ModelResponse,
} from "../../../../contracts/cortex-api";
import { GENERATION_DEFAULTS, resolveGenerationValues, type ParamKey } from "../../lib/generationParams";
import { displayModelName, isGGUFModel, localModelNames, modelFacts, modelSource } from "../../lib/localModels";
import { registerNavigationGuard } from "../../lib/navigation";
import { AlertDialog, DialogContent } from "../../shared/ui/Dialog";
import { Select } from "../../shared/ui/Select";
import {
  ContextWindowField,
  PresetPicker,
  SamplingField,
  SeedField,
  type GenerationPatch,
} from "../generation/GenerationControls";
import type { ModelProgress } from "../../stores/useModelStore";
import { MemoryPanel, type MemoryLoadState } from "./MemoryPanel";
import type { ListGGUFFiles } from "../models/GGUFDownloadForm";
import { ModelsPanel } from "../models/ModelsPanel";

type SettingsSection = "general" | "model" | "memory" | "translation" | "system";

export type SettingsPanelProps = {
  settings: CortexSettings;
  memos: string[];
  /** Absent means the list is already loaded. */
  memoryLoad?: MemoryLoadState;
  onRetryMemory?: () => void;
  saving: boolean;
  memoryBusy: boolean;
  /**
   * Resolves to the saved document, or `null` when nothing was saved. Leaving
   * the screen after "Save and close" depends on telling the two apart.
   */
  onSave: (settings: CortexSettings) => Promise<CortexSettings | null | void>;
  onAddMemory: (memo: string) => Promise<void>;
  onReplaceMemory: (memos: string[]) => Promise<void>;
  onClearMemory: () => Promise<void>;
  models: ModelResponse;
  modelBusy: boolean;
  modelProgress: ModelProgress | null;
  /** Stops the model operation whose progress is shown; without it the row has no Cancel. */
  onCancelModelJob?: () => void;
  setupUrl: string;
  onCheckModels: () => Promise<void>;
  onPullModel: (model: string) => Promise<void>;
  llamacppStatus: LlamaCppRuntimeStatus;
  onDownloadGGUF: (request: ModelDownloadRequest) => Promise<void>;
  /** Lists a Hugging Face repository's .gguf files; without it the download form only takes a typed file name. */
  onListHuggingFaceFiles?: ListGGUFFiles;
  /** Should leave through `navigate()`: that is where unsaved edits are asked about. */
  onClose: () => void;
};

const DEFAULT_TRANSLATION_MODEL = "translategemma:4b";

const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const valuesEqual = (left: unknown, right: unknown): boolean => {
  if (Object.is(left, right)) return true;
  if (!isRecord(left) || !isRecord(right)) return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => hasOwn(right, key) && valuesEqual(left[key], right[key]));
};

/**
 * Keep edits made in this dialog while adopting fields changed elsewhere.
 * Settings are saved as a document, so submitting the original draft would
 * otherwise replace a newer model/theme/revision with its stale values.
 */
const mergeChangedValues = <T,>(draft: T, baseline: T, current: T): T => {
  if (valuesEqual(draft, baseline)) return current;
  if (!isRecord(draft) || !isRecord(baseline)) return draft;

  const merged: Record<string, unknown> = isRecord(current) ? { ...current } : {};
  const keys = new Set([...Object.keys(draft), ...Object.keys(baseline)]);
  for (const key of keys) {
    if (!hasOwn(draft, key)) {
      // A deleted draft field is a local edit too. This is mostly defensive;
      // the current controls generally update values rather than delete keys.
      if (hasOwn(baseline, key)) delete merged[key];
      continue;
    }
  if (!hasOwn(baseline, key)) {
      merged[key] = draft[key];
      continue;
    }
    const currentValue = isRecord(current) ? current[key] : undefined;
    merged[key] = mergeChangedValues(draft[key], baseline[key], currentValue);
  }
  return merged as T;
};

const sections: { id: SettingsSection; label: string; detail: string }[] = [
  { id: "general", label: "General", detail: "Appearance and behavior" },
  { id: "model", label: "AI Model", detail: "Chat model and generation" },
  { id: "memory", label: "Memory", detail: "Saved local context" },
  { id: "translation", label: "Translation", detail: "Optional response translation" },
  { id: "system", label: "System", detail: "Runtime and installed models" },
];

export function SettingsPanel({
  settings,
  memos,
  memoryLoad,
  onRetryMemory,
  saving,
  memoryBusy,
  onSave,
  onAddMemory,
  onReplaceMemory,
  onClearMemory,
  models,
  modelBusy,
  modelProgress,
  onCancelModelJob,
  setupUrl,
  onCheckModels,
  onPullModel,
  llamacppStatus,
  onDownloadGGUF,
  onListHuggingFaceFiles,
  onClose,
}: SettingsPanelProps) {
  const [draft, setDraft] = useState(settings);
  const [baseline, setBaseline] = useState(settings);
  const [section, setSection] = useState<SettingsSection>("general");
  // Adopt fields changed elsewhere while keeping local edits. This happens
  // while rendering rather than in an effect so that `dirty` below is never
  // computed against a draft that has not caught up: an effect would show
  // "Unsaved changes" for a frame after every successful save.
  if (baseline !== settings) {
    setBaseline(settings);
    setDraft(mergeChangedValues(draft, baseline, settings));
  }
  const dirty = !valuesEqual(draft, settings);
  const dirtyRef = useRef(dirty);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  // Leaving with unsaved edits asks first. `navigate()` consults this guard,
  // which covers the sidebar, links, the command palette and the Close button.
  // Concurrent navigations share one prompt.
  const pendingLeaveRef = useRef<{ promise: Promise<boolean>; resolve: (allow: boolean) => void } | null>(null);
  const [leavePromptOpen, setLeavePromptOpen] = useState(false);
  const [leaveSaving, setLeaveSaving] = useState(false);
  const settleLeave = (allow: boolean) => {
    const pending = pendingLeaveRef.current;
    pendingLeaveRef.current = null;
    setLeavePromptOpen(false);
    pending?.resolve(allow);
  };
  useEffect(() => {
    const unregister = registerNavigationGuard(() => {
      if (!dirtyRef.current) return true;
      if (!pendingLeaveRef.current) {
        let resolve: (allow: boolean) => void = () => {};
        const promise = new Promise<boolean>((done) => { resolve = done; });
        pendingLeaveRef.current = { promise, resolve };
        setLeavePromptOpen(true);
      }
      return pendingLeaveRef.current.promise;
    });
    return () => {
      unregister();
      // If this screen goes away with the prompt open, stay put.
      pendingLeaveRef.current?.resolve(false);
      pendingLeaveRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!dirty) return undefined;
    // Closing the window or reloading is not a route change; let the browser
    // ask, as it does for any page with unsaved input.
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const installedModels = localModelNames(models);
  const appearance = draft.appearance ?? {};
  const generation = draft.generation ?? {};
  const execution = draft.execution ?? {};
  const modelSettings = draft.models ?? {};
  const memory = draft.memory ?? {};
  const translation = draft.translation ?? {};
  const selectedChatModel = installedModels.includes(modelSettings.chat ?? "")
    ? modelSettings.chat ?? ""
    : "";
  const configuredTranslationModel = modelSettings.translation ?? DEFAULT_TRANSLATION_MODEL;
  const selectedTranslationModel = installedModels.includes(configuredTranslationModel)
    ? configuredTranslationModel
    : "";

  const update = (next: Partial<CortexSettings>) => setDraft((current) => ({ ...current, ...next }));

  const generationValues = resolveGenerationValues(generation);
  const generationModified = (Object.keys(GENERATION_DEFAULTS) as ParamKey[])
    .some((key) => generationValues[key] !== GENERATION_DEFAULTS[key]);
  const setGeneration = (patch: GenerationPatch) => update({ generation: { ...generation, ...patch } });
  const generationFieldProps = { idPrefix: "settings", values: generationValues, defaults: GENERATION_DEFAULTS, onChange: setGeneration };

  const chooseChatModel = (chat: string) => update({ models: { ...modelSettings, chat, title: null } });

  const setTranslationEnabled = (enabled: boolean) => {
    const translationModel = installedModels.includes(configuredTranslationModel)
      ? configuredTranslationModel
      : installedModels[0] ?? configuredTranslationModel;
    update({
      translation: { ...translation, enabled },
      models: { ...modelSettings, translation: translationModel },
    });
  };

  const modelOptions = installedModels.map((model) => {
    const detail = models.models?.find((item) => item.name === model);
    return {
      value: model,
      label: isGGUFModel(model) ? `${displayModelName(model)} (GGUF)` : model,
      detail: [modelSource(model, detail) === "gguf" ? "GGUF file" : "Ollama", ...modelFacts(detail), ...(detail?.supports_vision ? ["Vision"] : [])].join(" · "),
    };
  });
  const saveDraft = async () => {
    const submitted = draft;
    const latest = mergeChangedValues(draft, baseline, settings);
    const latestModels = latest.models ?? {};
    const chatWasEdited = !valuesEqual(draft.models?.chat, baseline.models?.chat);
    const saved = await onSave({
      ...latest,
      models: {
        ...latestModels,
        // An empty inventory (Ollama down, a failed refresh) is a routine,
        // recoverable state -- it must not overwrite a still-valid configured
        // model with null just because the picker has nothing to offer right
        // now. Also preserve an externally selected model when this dialog
        // did not edit the chat picker, even if its inventory refresh lags.
        chat: chatWasEdited && installedModels.length ? (selectedChatModel || null) : (latestModels.chat ?? null),
        title: null,
      },
    });
    // The server may store a field differently from how it was typed (it trims
    // the target language, for one). Show what was stored for every field that
    // has not been edited since this save started, so a saved draft is clean.
    if (saved) setDraft((current) => mergeChangedValues(current, submitted, saved));
    return saved;
  };

  const saveAndLeave = async () => {
    setLeaveSaving(true);
    let saved: boolean;
    try {
      // `undefined` is a save that reported nothing; only an explicit null failed.
      saved = (await saveDraft()) !== null;
    } catch {
      saved = false;
    } finally {
      setLeaveSaving(false);
    }
    // A failed save leaves the edits on screen; the caller has already said why.
    settleLeave(saved);
  };

  return (
    <section className="settings-dialog" aria-labelledby="settings-title">
      <header className="settings-dialog-header">
        <div className="settings-title-group">
          <h2 id="settings-title">Settings</h2>
          <span aria-live="polite">
            {dirty && <span className="settings-dirty-pill">Unsaved changes</span>}
          </span>
        </div>
        <button className="icon-button icon-button-small" type="button" aria-label="Close settings" onClick={onClose}>
          <X aria-hidden="true" size={17} />
        </button>
      </header>

      <div className="settings-dialog-body">
        <nav className="settings-nav" aria-label="Settings categories">
          {sections.map((item) => {
            return <button
              className={`settings-tab ${section === item.id ? "settings-tab-active" : ""}`}
              type="button"
              key={item.id}
              aria-label={item.label}
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => setSection(item.id)}
            >
              <span><strong>{item.label}</strong><small>{item.detail}</small></span>
            </button>
          })}
        </nav>

        <div className="settings-pane">
          {section === "general" && (
            <section className="settings-section" aria-labelledby="general-settings-title">
              <div className="section-heading">
                <p className="eyebrow">GENERAL</p>
                <h3 id="general-settings-title">Appearance and responses</h3>
              </div>
              <div className="settings-form">
                <div className="field-label">
                  <span id="theme-label">Theme</span>
                  <Select
                    id="theme"
                    aria-labelledby="theme-label"
                    value={appearance.theme ?? "dark"}
                    options={[
                      { value: "system", label: "System" },
                      { value: "light", label: "Light" },
                      { value: "dark", label: "Dark" },
                    ]}
                    onChange={(theme) => update({ appearance: { ...appearance, theme: theme as "light" | "dark" | "system" } })}
                  />
                </div>
                <label className="toggle-row" htmlFor="automatic-compute">
                  <span><strong id="automatic-compute-label">Use safe computation automatically</strong><small id="automatic-compute-description">For explicit math requests, Cortex verifies the result locally before responding. General code always requires a separate approval.</small></span>
                  <input id="automatic-compute" type="checkbox" aria-labelledby="automatic-compute-label" aria-describedby="automatic-compute-description" checked={execution.automatic_compute ?? true} onChange={(event) => update({ execution: { ...execution, automatic_compute: event.target.checked } })} />
                </label>
                <label className="toggle-row" htmlFor="code-execution-enabled">
                  <span><strong id="code-execution-enabled-label">Allow local code requests</strong><small id="code-execution-enabled-description">Cortex may prepare a local Python task, but every run still pauses for your one-time approval.</small></span>
                  <input id="code-execution-enabled" type="checkbox" aria-labelledby="code-execution-enabled-label" aria-describedby="code-execution-enabled-description" checked={execution.code_execution_enabled ?? true} onChange={(event) => update({ execution: { ...execution, code_execution_enabled: event.target.checked } })} />
                </label>
              </div>
            </section>
          )}

          {section === "model" && (
            <section className="settings-section" aria-labelledby="model-settings-title">
              <div className="section-heading">
                <p className="eyebrow">AI MODEL</p>
                <h3 id="model-settings-title">Model and generation</h3>
              </div>
              <div className="settings-form">
                <section className="settings-group" aria-labelledby="chat-model-group">
                  <div className="settings-group-head">
                    <span>
                      <strong id="chat-model-group">Chat model</strong>
                      <small>Models installed through Ollama and local .gguf files. Chat titles use the same model.</small>
                    </span>
                  </div>
                  {installedModels.length > 0 ? (
                    <Select id="chat-model" aria-labelledby="chat-model-group" value={selectedChatModel} options={modelOptions} onChange={chooseChatModel} />
                  ) : (
                    <div className="model-selection-empty" role="status">
                      <strong>No local models found</strong>
                      <span>Install a model with Ollama, or add a .gguf file to your local models folder, then rescan this workspace.</span>
                      <button className="button button-secondary" type="button" onClick={() => void onCheckModels()} disabled={modelBusy}>Rescan local models</button>
                    </div>
                  )}
                </section>

                <section className="settings-group" aria-labelledby="generation-defaults-group">
                  <div className="settings-group-head">
                    <span>
                      <strong id="generation-defaults-group">Generation defaults</strong>
                      <small>Every chat starts from these. Parameters in the composer can adjust them for a single chat.</small>
                    </span>
                    {generationModified && (
                      <button className="params-reset" type="button" onClick={() => setGeneration(GENERATION_DEFAULTS)}>
                        <RotateCcw aria-hidden="true" size={12} />
                        <span>Restore defaults</span>
                      </button>
                    )}
                  </div>
                  <PresetPicker idPrefix="settings" values={generationValues} onChange={setGeneration} />
                  <div className="settings-range-grid">
                    <SamplingField field="temperature" {...generationFieldProps} />
                    <SamplingField field="top_p" {...generationFieldProps} />
                    <SamplingField field="top_k" {...generationFieldProps} />
                    <SamplingField field="repeat_penalty" {...generationFieldProps} />
                  </div>
                  <div className="settings-range-grid settings-range-grid-context">
                    <ContextWindowField {...generationFieldProps} />
                    <SeedField {...generationFieldProps} />
                  </div>
                </section>

                <section className="settings-group" aria-labelledby="system-prompt-group">
                  <div className="settings-group-head">
                    <span>
                      <strong id="system-prompt-group">System prompt</strong>
                      <small>Standing instructions sent with every message in every chat.</small>
                    </span>
                  </div>
                  <label className="field-label" htmlFor="system-instructions">System instructions
                    <textarea id="system-instructions" value={generation.system_instructions ?? ""} onChange={(event) => update({ generation: { ...generation, system_instructions: event.target.value } })} rows={4} />
                  </label>
                  <label className="toggle-row" htmlFor="bypass-system-prompt">
                    <span><strong id="bypass-system-prompt-label">Bypass Cortex's default system prompt</strong><small id="bypass-system-prompt-description">Skip Cortex's built-in identity and safety instructions. Only your system instructions above (if any) and the conversation are sent to the model.</small></span>
                    <input id="bypass-system-prompt" type="checkbox" aria-labelledby="bypass-system-prompt-label" aria-describedby="bypass-system-prompt-description" checked={generation.bypass_system_prompt ?? false} onChange={(event) => update({ generation: { ...generation, bypass_system_prompt: event.target.checked } })} />
                  </label>
                </section>
              </div>
            </section>
          )}

          {section === "memory" && (
            <section className="settings-section" aria-labelledby="memory-settings-title">
              <div className="section-heading">
                <p className="eyebrow">MEMORY</p>
                <h3 id="memory-settings-title">Permanent memory</h3>
              </div>
              <label className="toggle-row" htmlFor="memory-enabled">
                <span><strong>Use permanent memory</strong><small>Allow relevant saved facts in generation context.</small></span>
                <input id="memory-enabled" type="checkbox" checked={memory.enabled ?? true} onChange={(event) => update({ memory: { ...memory, enabled: event.target.checked } })} />
              </label>
              <MemoryPanel memos={memos} load={memoryLoad} onRetry={onRetryMemory} busy={memoryBusy} onAdd={onAddMemory} onReplace={onReplaceMemory} onClear={onClearMemory} />
            </section>
          )}

          {section === "translation" && (
            <section className="settings-section" aria-labelledby="translation-settings-title">
              <div className="section-heading">
                <p className="eyebrow">TRANSLATION</p>
                <h3 id="translation-settings-title">Response translation</h3>
              </div>
              <div className="settings-form">
                <label className="toggle-row" htmlFor="translation-enabled">
                  <span><strong>Translate responses</strong><small>Off by default. Translation never blocks normal chat.</small></span>
                  <input id="translation-enabled" type="checkbox" checked={translation.enabled ?? false} onChange={(event) => setTranslationEnabled(event.target.checked)} />
                </label>
                {translation.enabled && <>
                  <label className="field-label" htmlFor="target-language">Target language
                    <input id="target-language" value={translation.target_language ?? "Spanish"} onChange={(event) => update({ translation: { ...translation, target_language: event.target.value } })} />
                  </label>
                  {installedModels.length > 0 ? (
                    <div className="field-label">
                      <span id="translation-model-label">Translation model</span>
                      <Select id="translation-model" aria-labelledby="translation-model-label" value={selectedTranslationModel} options={modelOptions} placeholder={`${configuredTranslationModel} is not installed`} onChange={(translationModel) => update({ models: { ...modelSettings, translation: translationModel } })} />
                    </div>
                  ) : <p className="field-error">Install a local model before enabling translation.</p>}
                  {!installedModels.includes(DEFAULT_TRANSLATION_MODEL) && <div className={`translation-install${modelBusy ? " translation-install-active" : ""}`}>
                    <span><strong>Default translation model</strong><small>{DEFAULT_TRANSLATION_MODEL} is optional and is only used when translation is enabled.</small></span>
                    <button
                      className="button button-secondary translation-install-button"
                      type="button"
                      onClick={() => void onPullModel(DEFAULT_TRANSLATION_MODEL)}
                      disabled={modelBusy}
                      aria-busy={modelBusy}
                    >
                      {modelBusy && <span className="loading-spinner button-loading-spinner" aria-hidden="true" />}
                      {modelBusy ? (modelProgress?.model === DEFAULT_TRANSLATION_MODEL ? "Installing…" : "Working…") : "Install default"}
                    </button>
                    {modelBusy && (
                      <div className="translation-install-status" role="status" aria-label="Model installation status" aria-live="polite">
                        <span className="loading-spinner translation-install-status-spinner" aria-hidden="true" />
                        <span className="translation-install-status-copy">
                          {modelProgress?.model === DEFAULT_TRANSLATION_MODEL ? modelProgress.status : "Checking local model availability…"}
                        </span>
                        {modelProgress?.model === DEFAULT_TRANSLATION_MODEL && modelProgress.percent !== null && <strong>{modelProgress.percent}%</strong>}
                      </div>
                    )}
                  </div>}
                </>}
              </div>
            </section>
          )}

          {section === "system" && (
            <ModelsPanel
              models={models}
              busy={modelBusy}
              progress={modelProgress}
              onCancel={onCancelModelJob}
              setupUrl={setupUrl}
              onCheck={onCheckModels}
              llamacppStatus={llamacppStatus}
              gguf={{
                directory: modelSettings.gguf_directory ?? "",
                directoryDirty: (modelSettings.gguf_directory ?? "") !== (settings.models?.gguf_directory ?? ""),
                onDirectoryChange: (value) => update({ models: { ...modelSettings, gguf_directory: value || null } }),
                onDownload: onDownloadGGUF,
                busy: modelBusy,
                onListFiles: onListHuggingFaceFiles,
              }}
            />
          )}
        </div>
      </div>

      <footer className="settings-dialog-footer">
        <button className="button button-secondary" type="button" onClick={onClose}>Close</button>
        <button className="button button-primary" type="button" onClick={() => void saveDraft()} disabled={saving || !dirty}>
          <Save aria-hidden="true" size={16} /> {saving ? "Saving..." : "Save settings"}
        </button>
      </footer>

      {leavePromptOpen && (
        <AlertDialog.Root open onOpenChange={(open) => { if (!open && !leaveSaving) settleLeave(false); }}>
          <DialogContent>
            <AlertDialog.Title>Save your changes?</AlertDialog.Title>
            <AlertDialog.Description className="delete-dialog-description">
              You have unsaved changes to your settings. Leaving now would discard them.
            </AlertDialog.Description>
            <div className="dialog-actions">
              <button type="button" className="button button-secondary" onClick={() => settleLeave(false)} disabled={leaveSaving}>Keep editing</button>
              <button type="button" className="button button-danger" onClick={() => settleLeave(true)} disabled={leaveSaving}>Discard</button>
              <button type="button" className="button button-primary" onClick={() => void saveAndLeave()} disabled={leaveSaving}>
                {leaveSaving ? "Saving..." : "Save and close"}
              </button>
            </div>
          </DialogContent>
        </AlertDialog.Root>
      )}
    </section>
  );
}

