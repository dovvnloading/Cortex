import { Check, CircleAlert, Cpu, ExternalLink, RefreshCw } from "lucide-react";
import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { formatModelSize, localModelNames } from "../lib/localModels";

type Props = {
  models: ModelResponse;
  settings: CortexSettings;
  busy: boolean;
  setupUrl: string;
  onRescan: () => Promise<void>;
  onSelectModel: (model: string) => Promise<boolean>;
};

export function LocalSetup({ models, settings, busy, setupUrl, onRescan, onSelectModel }: Props) {
  const localModels = useMemo(() => localModelNames(models), [models]);
  const [selection, setSelection] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const choiceRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const connectionReady = models.connection?.success ?? true;
  const previousModel = settings.models?.chat?.trim() || null;
  const selectedModel = localModels.includes(selection) ? selection : "";

  const confirmSelection = async () => {
    if (!selectedModel || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await onSelectModel(selectedModel);
      if (!saved) setError("Cortex could not save that model selection. Please try again.");
    } finally {
      setSaving(false);
    }
  };

  const handleModelChoiceKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const lastIndex = localModels.length - 1;
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = index === lastIndex ? 0 : index + 1;
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = index === 0 ? lastIndex : index - 1;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = lastIndex;
    if (nextIndex === null) return;
    event.preventDefault();
    setSelection(localModels[nextIndex]);
    window.requestAnimationFrame(() => choiceRefs.current[nextIndex ?? index]?.focus());
  };

  if (!connectionReady) {
    return (
      <main className="local-setup" aria-labelledby="local-setup-title">
        <section className="local-setup-card local-setup-card-alert">
          <div className="local-setup-heading">
            <CircleAlert aria-hidden="true" size={21} />
            <h1 id="local-setup-title">Ollama is unavailable</h1>
          </div>
          <p className="lede">Start Ollama, then rescan to make your installed models available.</p>
          <div className="local-setup-status local-setup-status-alert" role="status">
            <span>{models.connection?.message ?? "Cortex could not reach Ollama."}</span>
          </div>
          <div className="local-setup-actions">
            <button className="button button-secondary" type="button" onClick={() => void onRescan()} disabled={busy}>
              <RefreshCw aria-hidden="true" size={16} /> {busy ? "Checking..." : "Rescan local runtime"}
            </button>
            <a className="button button-primary" href={setupUrl} target="_blank" rel="noreferrer">
              Get Ollama <ExternalLink aria-hidden="true" size={15} />
            </a>
          </div>
        </section>
      </main>
    );
  }

  if (!localModels.length) {
    return (
      <main className="local-setup" aria-labelledby="local-setup-title">
        <section className="local-setup-card">
          <h1 id="local-setup-title">No local models found</h1>
          <p className="lede">Install a model in Ollama, then rescan. Cortex will list it here.</p>
          <div className="local-setup-status" role="status"><Cpu aria-hidden="true" size={17} /> <span>Waiting for the local model inventory.</span></div>
          <div className="local-setup-actions">
            <button className="button button-secondary" type="button" onClick={() => void onRescan()} disabled={busy}>
              <RefreshCw aria-hidden="true" size={16} /> {busy ? "Scanning..." : "Rescan local models"}
            </button>
            <a className="button button-primary" href={setupUrl} target="_blank" rel="noreferrer">
              Open Ollama <ExternalLink aria-hidden="true" size={15} />
            </a>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="local-setup" aria-labelledby="local-setup-title">
      <section className="local-setup-card local-setup-model-card">
        <h1 id="local-setup-title">Select a local model</h1>
        <p className="lede">
          Choose an installed model for chat. You can change it later in Settings.
          {previousModel && !localModels.includes(previousModel) ? ` ${previousModel} is no longer available, so choose a replacement.` : ""}
        </p>
        <div className="model-choice-list" role="radiogroup" aria-label="Installed local models">
          {localModels.map((model, index) => {
            const detail = models.models?.find((item) => item.name === model);
            const size = formatModelSize(detail?.size);
            const selected = selectedModel === model;
            return (
              <button
                className={`model-choice ${selected ? "model-choice-selected" : ""}`}
                key={model}
                type="button"
                role="radio"
                aria-checked={selected}
                tabIndex={selected ? 0 : selectedModel ? -1 : index === 0 ? 0 : -1}
                ref={(node) => { choiceRefs.current[index] = node; }}
                onKeyDown={(event) => handleModelChoiceKeyDown(event, index)}
                onClick={() => { setSelection(model); setError(null); }}
              >
                <span className="model-choice-icon"><Cpu aria-hidden="true" size={17} /></span>
                <span className="model-choice-copy"><strong>{model}</strong><small>{size ? `${size} installed locally` : "Installed locally"}</small></span>
                {selected && <Check className="model-choice-check" aria-hidden="true" size={18} />}
              </button>
            );
          })}
        </div>
        {error && <p className="field-error" role="alert">{error}</p>}
        <div className="local-setup-actions local-setup-actions-bottom">
          <button className="button button-secondary" type="button" onClick={() => void onRescan()} disabled={busy || saving}>
            <RefreshCw aria-hidden="true" size={16} /> Rescan
          </button>
          <button className="button button-primary" type="button" onClick={() => void confirmSelection()} disabled={!selectedModel || busy || saving}>
            {saving ? "Saving..." : "Use selected model"}
          </button>
        </div>
      </section>
    </main>
  );
}
