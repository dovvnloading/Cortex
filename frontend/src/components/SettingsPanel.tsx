import { Save, X } from "lucide-react";
import { useState } from "react";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { MemoryPanel } from "./MemoryPanel";
import { ModelsPanel } from "./ModelsPanel";

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
  onClose: () => void;
};

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
  const appearance = draft.appearance ?? {};
  const generation = draft.generation ?? {};
  const modelSettings = draft.models ?? {};
  const memory = draft.memory ?? {};
  const translation = draft.translation ?? {};
  const suggestions = draft.suggestions ?? {};

  const update = (next: Partial<CortexSettings>) => setDraft((current) => ({ ...current, ...next }));

  return (
    <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
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
                <label className="field-label" htmlFor="theme">Theme
                  <select id="theme" value={appearance.theme ?? "system"} onChange={(event) => update({ appearance: { ...appearance, theme: event.target.value as "light" | "dark" | "system" } })}>
                    <option value="system">System</option>
                    <option value="light">Light</option>
                    <option value="dark">Dark</option>
                  </select>
                </label>
                <label className="toggle-row" htmlFor="suggestions-enabled">
                  <span><strong>Follow-up suggestions</strong><small>Offer short next-step prompts after responses.</small></span>
                  <input id="suggestions-enabled" type="checkbox" checked={suggestions.enabled ?? true} onChange={(event) => update({ suggestions: { ...suggestions, enabled: event.target.checked } })} />
                </label>
              </div>
            </section>
          )}

          {section === "model" && (
            <section className="settings-section" aria-labelledby="model-settings-title">
              <div className="section-heading">
                <p className="eyebrow">AI MODEL</p>
                <h3 id="model-settings-title">Model behavior</h3>
              </div>
              <div className="settings-form">
                <label className="field-label" htmlFor="chat-model">Chat model tag
                  <input id="chat-model" value={modelSettings.chat ?? "qwen3:8b"} onChange={(event) => update({ models: { ...modelSettings, chat: event.target.value } })} maxLength={200} />
                </label>
                <label className="field-label" htmlFor="title-model">Title model tag
                  <input id="title-model" value={modelSettings.title ?? "granite4:tiny-h"} onChange={(event) => update({ models: { ...modelSettings, title: event.target.value } })} maxLength={200} />
                </label>
                <label className="field-label" htmlFor="suggestions-model">Suggestions model tag
                  <input id="suggestions-model" value={suggestions.model ?? modelSettings.chat ?? "qwen3:8b"} onChange={(event) => update({ suggestions: { ...suggestions, model: event.target.value } })} maxLength={200} />
                </label>
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
                  <span><strong>Translate responses</strong><small>Translate completed responses into a target language.</small></span>
                  <input id="translation-enabled" type="checkbox" checked={translation.enabled ?? false} onChange={(event) => update({ translation: { ...translation, enabled: event.target.checked } })} />
                </label>
                <label className="field-label" htmlFor="target-language">Target language
                  <input id="target-language" value={translation.target_language ?? "Spanish"} onChange={(event) => update({ translation: { ...translation, target_language: event.target.value } })} />
                </label>
                <label className="field-label" htmlFor="translation-model">Translation model tag
                  <input id="translation-model" value={modelSettings.translation ?? "translategemma:4b"} onChange={(event) => update({ models: { ...modelSettings, translation: event.target.value } })} maxLength={200} />
                </label>
              </div>
            </section>
          )}

          {section === "system" && (
            <ModelsPanel models={models} busy={modelBusy} progress={modelProgress} setupUrl={setupUrl} onCheck={onCheckModels} onPull={onPullModel} />
          )}
        </div>
      </div>

      <footer className="settings-dialog-footer">
        <button className="button button-secondary" type="button" onClick={onClose}>Close</button>
        <button className="button button-primary" type="button" onClick={() => void onSave(draft)} disabled={saving}>
          <Save aria-hidden="true" size={16} /> {saving ? "Saving…" : "Save settings"}
        </button>
      </footer>
    </section>
  );
}
