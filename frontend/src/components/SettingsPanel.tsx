import { Save, Settings2 } from "lucide-react";
import { useState } from "react";
import type { CortexSettings } from "../../../contracts/cortex-api";
import { MemoryPanel } from "./MemoryPanel";

type Props = {
  settings: CortexSettings;
  memos: string[];
  saving: boolean;
  memoryBusy: boolean;
  onSave: (settings: CortexSettings) => Promise<void>;
  onAddMemory: (memo: string) => Promise<void>;
  onClearMemory: () => Promise<void>;
};

export function SettingsPanel({
  settings,
  memos,
  saving,
  memoryBusy,
  onSave,
  onAddMemory,
  onClearMemory,
}: Props) {
  const [draft, setDraft] = useState(settings);
  const appearance = draft.appearance ?? {};
  const generation = draft.generation ?? {};
  const memory = draft.memory ?? {};
  const translation = draft.translation ?? {};

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
        </div>
        <button className="button button-primary" onClick={() => void onSave(draft)} disabled={saving}>
          <Save aria-hidden="true" size={16} /> {saving ? "Saving…" : "Save settings"}
        </button>
      </section>
      <MemoryPanel memos={memos} busy={memoryBusy} onAdd={onAddMemory} onClear={onClearMemory} />
    </div>
  );
}
