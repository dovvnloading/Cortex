import { Check, ChevronDown, Save, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { localModelNames } from "../lib/localModels";
import { MemoryPanel } from "./MemoryPanel";
import { ModelsPanel } from "./ModelsPanel";

type SettingsSection = "general" | "model" | "memory" | "translation" | "system";

type PickerOption = {
  value: string;
  label: string;
  detail?: string;
};

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
  onClose: () => void;
};

const DEFAULT_TRANSLATION_MODEL = "translategemma:4b";

const sections: { id: SettingsSection; label: string }[] = [
  { id: "general", label: "General" },
  { id: "model", label: "AI Model" },
  { id: "memory", label: "Memory" },
  { id: "translation", label: "Translation" },
  { id: "system", label: "System" },
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
  onClose,
}: SettingsPanelProps) {
  const [draft, setDraft] = useState(settings);
  const [section, setSection] = useState<SettingsSection>("general");
  const installedModels = localModelNames(models);
  const appearance = draft.appearance ?? {};
  const generation = draft.generation ?? {};
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

  const modelOptions = installedModels.map((model) => ({ value: model, label: model, detail: "Installed locally" }));
  const saveDraft = () => onSave({
    ...draft,
    models: { ...modelSettings, chat: selectedChatModel || null, title: null },
  });

  return (
    <section className="settings-dialog" aria-labelledby="settings-title">
      <header className="settings-dialog-header">
        <h2 id="settings-title">Settings</h2>
        <button className="icon-button icon-button-small" type="button" aria-label="Close settings" onClick={onClose}>
          <X aria-hidden="true" size={17} />
        </button>
      </header>

      <div className="settings-dialog-body">
        <nav className="settings-nav" aria-label="Settings categories">
          {sections.map((item) => (
            <button
              className={`settings-tab ${section === item.id ? "settings-tab-active" : ""}`}
              type="button"
              key={item.id}
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => setSection(item.id)}
            >
              {item.label}
            </button>
          ))}
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
                  <RoundedPicker
                    id="theme"
                    labelledBy="theme-label"
                    value={appearance.theme ?? "dark"}
                    options={[
                      { value: "system", label: "System" },
                      { value: "light", label: "Light" },
                      { value: "dark", label: "Dark" },
                    ]}
                    onChange={(theme) => update({ appearance: { ...appearance, theme: theme as "light" | "dark" | "system" } })}
                  />
                </div>
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
                <p className="model-selection-note">Cortex scans the Ollama models installed on this PC. Select the model to use for chat; automatic chat titles use the same local model.</p>
                {installedModels.length > 0 ? (
                  <div className="field-label">
                    <span id="chat-model-label">Chat model</span>
                    <RoundedPicker id="chat-model" labelledBy="chat-model-label" value={selectedChatModel} options={modelOptions} onChange={chooseChatModel} />
                  </div>
                ) : (
                  <div className="model-selection-empty" role="status">
                    <strong>No local models found</strong>
                    <span>Install a model with Ollama, then rescan this workspace.</span>
                    <button className="button button-secondary" type="button" onClick={() => void onCheckModels()} disabled={modelBusy}>Rescan local models</button>
                  </div>
                )}
                <label className="field-label" htmlFor="temperature">Temperature <span className="field-value">{generation.temperature ?? 0.7}</span>
                  <input id="temperature" type="range" min="0" max="2" step="0.1" value={generation.temperature ?? 0.7} onChange={(event) => update({ generation: { ...generation, temperature: Number(event.target.value) } })} />
                </label>
                <div className="settings-field-row">
                  <label className="field-label" htmlFor="num-ctx">Context window
                    <input id="num-ctx" type="number" min="2048" max="16384" step="1024" value={generation.num_ctx ?? 4096} onChange={(event) => update({ generation: { ...generation, num_ctx: Number(event.target.value) } })} />
                  </label>
                  <label className="field-label" htmlFor="seed">Seed
                    <input id="seed" type="number" min="-1" max="2147483647" value={generation.seed ?? -1} onChange={(event) => update({ generation: { ...generation, seed: Number(event.target.value) } })} />
                  </label>
                </div>
                <label className="field-label" htmlFor="system-instructions">System instructions
                  <textarea id="system-instructions" value={generation.system_instructions ?? ""} onChange={(event) => update({ generation: { ...generation, system_instructions: event.target.value } })} maxLength={1800} rows={4} />
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
                      <RoundedPicker id="translation-model" labelledBy="translation-model-label" value={selectedTranslationModel} options={modelOptions} placeholder={`${configuredTranslationModel} is not installed`} onChange={(translationModel) => update({ models: { ...modelSettings, translation: translationModel } })} />
                    </div>
                  ) : <p className="field-error">Install a local model before enabling translation.</p>}
                  {!installedModels.includes(DEFAULT_TRANSLATION_MODEL) && <div className="translation-install">
                    <span><strong>Default translation model</strong><small>{DEFAULT_TRANSLATION_MODEL} is optional and is only used when translation is enabled.</small></span>
                    <button className="button button-secondary" type="button" onClick={() => void onPullModel(DEFAULT_TRANSLATION_MODEL)} disabled={modelBusy}>Install default</button>
                  </div>}
                </>}
              </div>
            </section>
          )}

          {section === "system" && (
            <ModelsPanel models={models} busy={modelBusy} progress={modelProgress} setupUrl={setupUrl} onCheck={onCheckModels} />
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

function RoundedPicker({ id, labelledBy, value, options, placeholder = "Choose a model", onChange, disabled = false }: { id: string; labelledBy: string; value: string; options: PickerOption[]; placeholder?: string; onChange: (value: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const listId = useId();
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : undefined;
  const initialIndex = selectedIndex >= 0 ? selectedIndex : 0;

  useEffect(() => {
    if (!open) return undefined;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const focusOption = (index: number) => {
    setActiveIndex(index);
    setOpen(true);
    window.requestAnimationFrame(() => optionRefs.current[index]?.focus());
  };

  const closePicker = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const selectOption = (index: number) => {
    onChange(options[index].value);
    closePicker(true);
  };

  const nextOptionIndex = (index: number, direction: 1 | -1) => (index + direction + options.length) % options.length;

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!options.length) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePicker();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusOption(open ? nextOptionIndex(activeIndex, 1) : initialIndex);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      focusOption(open ? nextOptionIndex(activeIndex, -1) : selectedIndex >= 0 ? nextOptionIndex(selectedIndex, -1) : options.length - 1);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) closePicker();
      else focusOption(initialIndex);
    }
  };

  const handleOptionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closePicker(true);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      event.preventDefault();
      focusOption(nextOptionIndex(index, 1));
      return;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      event.preventDefault();
      focusOption(nextOptionIndex(index, -1));
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      focusOption(0);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      focusOption(options.length - 1);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectOption(index);
    }
  };

  return (
    <div className={`rounded-picker ${open ? "rounded-picker-open" : ""}`} ref={rootRef}>
      <button ref={triggerRef} id={id} className="rounded-picker-trigger" type="button" aria-labelledby={labelledBy} aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} onClick={() => { if (open) closePicker(); else { setActiveIndex(initialIndex); setOpen(true); } }} onKeyDown={handleTriggerKeyDown} disabled={disabled || options.length === 0}>
        <span className="rounded-picker-selection">
          <strong>{selected?.label ?? placeholder}</strong>
          {selected?.detail && <small>{selected.detail}</small>}
        </span>
        <ChevronDown aria-hidden="true" size={17} />
      </button>
      {open && <div id={listId} className="rounded-picker-list" role="listbox" aria-labelledby={labelledBy}>
        {options.map((option, index) => <button key={option.value} ref={(node) => { optionRefs.current[index] = node; }} className={`rounded-picker-option ${option.value === value ? "rounded-picker-option-active" : ""}`} type="button" role="option" aria-label={option.label} aria-selected={option.value === value} tabIndex={index === activeIndex ? 0 : -1} onFocus={() => setActiveIndex(index)} onKeyDown={(event) => handleOptionKeyDown(event, index)} onClick={() => selectOption(index)}>
          <span><strong>{option.label}</strong>{option.detail && <small>{option.detail}</small>}</span>
          {option.value === value && <Check aria-hidden="true" size={16} />}
        </button>)}
      </div>}
    </div>
  );
}
