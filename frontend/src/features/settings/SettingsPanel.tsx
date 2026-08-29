import { Save, X } from "lucide-react";
import { useState } from "react";
import type {
  CortexSettings,
  LlamaCppRuntimeStatus,
  ModelDownloadRequest,
  ModelResponse,
} from "../../../../contracts/cortex-api";
import { displayModelName, isGGUFModel, localModelNames } from "../../lib/localModels";
import { RangeField } from "../../shared/ui/RangeField";
import { Select } from "../../shared/ui/Select";
import { MemoryPanel } from "./MemoryPanel";
import { ModelsPanel } from "../models/ModelsPanel";

type SettingsSection = "general" | "model" | "memory" | "translation" | "system";

export type SettingsPanelProps = {
  settings: CortexSettings;
  memos: string[];
  saving: boolean;
  memoryBusy: boolean;
  onSave: (settings: CortexSettings) => Promise<void>;
  onAddMemory: (memo: string) => Promise<void>;
  onReplaceMemory: (memos: string[]) => Promise<void>;
  onClearMemory: () => Promise<void>;
  models: ModelResponse;
  modelBusy: boolean;
  modelProgress: { model: string; status: string; percent: number | null } | null;
  setupUrl: string;
  onCheckModels: () => Promise<void>;
  onPullModel: (model: string) => Promise<void>;
  llamacppStatus: LlamaCppRuntimeStatus;
  onDownloadGGUF: (request: ModelDownloadRequest) => Promise<void>;
  onClose: () => void;
};

const DEFAULT_TRANSLATION_MODEL = "translategemma:4b";

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
  saving,
  memoryBusy,
  onSave,
  onAddMemory,
  onReplaceMemory,
  onClearMemory,
  models,
  modelBusy,
  modelProgress,
  setupUrl,
  onCheckModels,
  onPullModel,
  llamacppStatus,
  onDownloadGGUF,
  onClose,
}: SettingsPanelProps) {
  const [draft, setDraft] = useState(settings);
  const [section, setSection] = useState<SettingsSection>("general");
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

  const modelOptions = installedModels.map((model) => ({
    value: model,
    label: isGGUFModel(model) ? `${displayModelName(model)} (GGUF)` : model,
    detail: "Installed locally",
  }));
  const saveDraft = () => onSave({
    ...draft,
    models: {
      ...modelSettings,
      // An empty inventory (Ollama down, a failed refresh) is a routine,
      // recoverable state -- it must not overwrite a still-valid configured
      // model with null just because the picker has nothing to offer right now.
      chat: installedModels.length ? (selectedChatModel || null) : (modelSettings.chat ?? null),
      title: null,
    },
  });

  return (
    <section className="settings-dialog" aria-labelledby="settings-title">
      <header className="settings-dialog-header">
        <div className="settings-title-group">
          <h2 id="settings-title">Settings</h2>
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
                <h3 id="model-settings-title">Local model selection</h3>
              </div>
              <div className="settings-form">
                <p className="model-selection-note">Cortex lists models installed through Ollama and local .gguf files. Select the model to use for chat; automatic chat titles use the same local model.</p>
                {installedModels.length > 0 ? (
                  <div className="field-label">
                    <span id="chat-model-label">Chat model</span>
                    <Select id="chat-model" aria-labelledby="chat-model-label" value={selectedChatModel} options={modelOptions} onChange={chooseChatModel} />
                  </div>
                ) : (
                  <div className="model-selection-empty" role="status">
                    <strong>No local models found</strong>
                    <span>Install a model with Ollama, or add a .gguf file to your local models folder, then rescan this workspace.</span>
                    <button className="button button-secondary" type="button" onClick={() => void onCheckModels()} disabled={modelBusy}>Rescan local models</button>
                  </div>
                )}
                <hr className="settings-divider" />
                <div className="settings-subhead">
                  <strong>Sampling</strong>
                  <small>How the model picks its next token. Defaults suit most local models.</small>
                </div>
                <div className="settings-range-grid">
                  <RangeField id="temperature" label="Temperature" min={0} max={2} step={0.1} value={generation.temperature ?? 0.7} format={(value) => value.toFixed(1)} onChange={(temperature) => update({ generation: { ...generation, temperature } })} />
                  <RangeField id="top-p" label="Top P" min={0} max={1} step={0.05} value={generation.top_p ?? 0.9} format={(value) => value.toFixed(2)} onChange={(top_p) => update({ generation: { ...generation, top_p } })} />
                  <RangeField id="top-k" label="Top K" min={0} max={200} step={1} value={generation.top_k ?? 40} onChange={(top_k) => update({ generation: { ...generation, top_k } })} />
                  <RangeField id="repeat-penalty" label="Repeat penalty" min={0.5} max={2} step={0.05} value={generation.repeat_penalty ?? 1.1} format={(value) => value.toFixed(2)} onChange={(repeat_penalty) => update({ generation: { ...generation, repeat_penalty } })} />
                </div>

                <hr className="settings-divider" />
                <div className="settings-subhead">
                  <strong>Context</strong>
                  <small>A larger window holds more conversation but uses more memory. Seed -1 keeps replies varied.</small>
                </div>
                <div className="settings-field-row">
                  <label className="field-label" htmlFor="num-ctx">Context window
                    <input id="num-ctx" type="number" min="2048" max="65536" step="1024" value={generation.num_ctx ?? 8192} onChange={(event) => update({ generation: { ...generation, num_ctx: Number(event.target.value) } })} />
                  </label>
                  <label className="field-label" htmlFor="seed">Seed
                    <input id="seed" type="number" min="-1" max="2147483647" value={generation.seed ?? -1} onChange={(event) => update({ generation: { ...generation, seed: Number(event.target.value) } })} />
                  </label>
                </div>

                <hr className="settings-divider" />
                <div className="settings-subhead">
                  <strong>System prompt</strong>
                  <small>Standing instructions sent with every message in every chat.</small>
                </div>
                <label className="field-label" htmlFor="system-instructions">System instructions
                  <textarea id="system-instructions" value={generation.system_instructions ?? ""} onChange={(event) => update({ generation: { ...generation, system_instructions: event.target.value } })} rows={4} />
                </label>
                <label className="toggle-row" htmlFor="bypass-system-prompt">
                  <span><strong id="bypass-system-prompt-label">Bypass Cortex's default system prompt</strong><small id="bypass-system-prompt-description">Skip Cortex's built-in identity and safety instructions. Only your system instructions above (if any) and the conversation are sent to the model.</small></span>
                  <input id="bypass-system-prompt" type="checkbox" aria-labelledby="bypass-system-prompt-label" aria-describedby="bypass-system-prompt-description" checked={generation.bypass_system_prompt ?? false} onChange={(event) => update({ generation: { ...generation, bypass_system_prompt: event.target.checked } })} />
                </label>
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
              <MemoryPanel memos={memos} busy={memoryBusy} onAdd={onAddMemory} onReplace={onReplaceMemory} onClear={onClearMemory} />
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
              setupUrl={setupUrl}
              onCheck={onCheckModels}
              llamacppStatus={llamacppStatus}
              gguf={{
                directory: modelSettings.gguf_directory ?? "",
                onDirectoryChange: (value) => update({ models: { ...modelSettings, gguf_directory: value || null } }),
                onDownload: onDownloadGGUF,
                busy: modelBusy,
              }}
            />
          )}
        </div>
      </div>

      <footer className="settings-dialog-footer">
        <button className="button button-secondary" type="button" onClick={onClose}>Close</button>
        <button className="button button-primary" type="button" onClick={() => void saveDraft()} disabled={saving}>
          <Save aria-hidden="true" size={16} /> {saving ? "Saving..." : "Save settings"}
        </button>
      </footer>
    </section>
  );
}

