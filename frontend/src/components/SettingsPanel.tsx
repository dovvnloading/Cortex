import { Save, Settings2 } from "lucide-react";
import { useState } from "react";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { MemoryPanel } from "./MemoryPanel";
import { ModelsPanel } from "./ModelsPanel";

type Props = {
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
};

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
}: Props) {
  const [draft, setDraft] = useState(settings);
  const appearance = draft.appearance ?? {};
  const generation = draft.generation ?? {};
  const modelSettings = draft.models ?? {};
  const memory = draft.memory ?? {};
  const translation = draft.translation ?? {};
  const suggestions = draft.suggestions ?? {};

  const update = (next: Partial<CortexSettings>) => setDraft((current) => ({ ...current, ...next }));

  return (
    <div className="settings-layout">
      <section className="panel" aria-labelledby="settings-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">WORKSPACE SETTINGS</p>
            <h2 id="settings-title">Control the local experience</h2>
          </div>
          <Settings2 aria-hidden="true" size={19} />
        </div>
        <div className="settings-form">
          <label className="field-label" htmlFor="theme">Theme
            <select id="theme" value={appearance.theme ?? "system"} onChange={(event) => update({ appearance: { ...appearance, theme: event.target.value as "light" | "dark" | "system" } })}>
              <option value="system">System</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
          <label className="field-label" htmlFor="temperature">Temperature <span className="field-value">{generation.temperature ?? 0.7}</span>
            <input id="temperature" type="range" min="0" max="2" step="0.1" value={generation.temperature ?? 0.7} onChange={(event) => update({ generation: { ...generation, temperature: Number(event.target.value) } })} />
          </label>
          <label className="field-label" htmlFor="num-ctx">Context window
            <input id="num-ctx" type="number" min="2048" max="16384" step="1024" value={generation.num_ctx ?? 4096} onChange={(event) => update({ generation: { ...generation, num_ctx: Number(event.target.value) } })} />
          </label>
          <label className="field-label" htmlFor="seed">Seed
            <input id="seed" type="number" min="-1" max="2147483647" value={generation.seed ?? -1} onChange={(event) => update({ generation: { ...generation, seed: Number(event.target.value) } })} />
          </label>
          <label className="field-label" htmlFor="system-instructions">System instructions
            <textarea id="system-instructions" value={generation.system_instructions ?? ""} onChange={(event) => update({ generation: { ...generation, system_instructions: event.target.value } })} maxLength={1800} rows={4} />
          </label>
          <label className="field-label" htmlFor="chat-model">Chat model tag
            <input id="chat-model" value={modelSettings.chat ?? "qwen3:8b"} onChange={(event) => update({ models: { ...modelSettings, chat: event.target.value } })} maxLength={200} />
          </label>
          <label className="field-label" htmlFor="title-model">Title model tag
            <input id="title-model" value={modelSettings.title ?? "granite4:tiny-h"} onChange={(event) => update({ models: { ...modelSettings, title: event.target.value } })} maxLength={200} />
          </label>
          <label className="toggle-row" htmlFor="memory-enabled">
            <span><strong>Permanent memory</strong><small>Allow relevant saved facts in generation context.</small></span>
            <input id="memory-enabled" type="checkbox" checked={memory.enabled ?? true} onChange={(event) => update({ memory: { ...memory, enabled: event.target.checked } })} />
          </label>
          <label className="toggle-row" htmlFor="translation-enabled">
            <span><strong>Translation</strong><small>Translate completed responses into a target language.</small></span>
            <input id="translation-enabled" type="checkbox" checked={translation.enabled ?? false} onChange={(event) => update({ translation: { ...translation, enabled: event.target.checked } })} />
          </label>
          <label className="field-label" htmlFor="target-language">Target language
            <input id="target-language" value={translation.target_language ?? "Spanish"} onChange={(event) => update({ translation: { ...translation, target_language: event.target.value } })} />
          </label>
          <label className="field-label" htmlFor="translation-model">Translation model tag
            <input id="translation-model" value={modelSettings.translation ?? "translategemma:4b"} onChange={(event) => update({ models: { ...modelSettings, translation: event.target.value } })} maxLength={200} />
          </label>
          <label className="toggle-row" htmlFor="suggestions-enabled">
            <span><strong>Follow-up suggestions</strong><small>Offer short next-step prompts after responses.</small></span>
            <input id="suggestions-enabled" type="checkbox" checked={suggestions.enabled ?? true} onChange={(event) => update({ suggestions: { ...suggestions, enabled: event.target.checked } })} />
          </label>
          <label className="field-label" htmlFor="suggestions-model">Suggestions model tag
            <input id="suggestions-model" value={suggestions.model ?? modelSettings.chat ?? "qwen3:8b"} onChange={(event) => update({ suggestions: { ...suggestions, model: event.target.value } })} maxLength={200} />
          </label>
        </div>
        <button className="button button-primary" onClick={() => void onSave(draft)} disabled={saving}>
          <Save aria-hidden="true" size={16} /> {saving ? "Saving…" : "Save settings"}
        </button>
      </section>
      <MemoryPanel memos={memos} busy={memoryBusy} onAdd={onAddMemory} onReplace={onReplaceMemory} onClear={onClearMemory} />
      <ModelsPanel models={models} busy={modelBusy} progress={modelProgress} setupUrl={setupUrl} onCheck={onCheckModels} onPull={onPullModel} />
    </div>
  );
}
