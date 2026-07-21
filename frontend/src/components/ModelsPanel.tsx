import { ExternalLink, RefreshCw, Cpu } from "lucide-react";
import type { ModelResponse } from "../../../contracts/cortex-api";

type Progress = {
  model: string;
  status: string;
  percent: number | null;
};

type Props = {
  models: ModelResponse;
  busy: boolean;
  progress: Progress | null;
  setupUrl: string;
  onCheck: () => Promise<void>;
};

export function ModelsPanel({ models, busy, progress, setupUrl, onCheck }: Props) {
  const connection = models.connection;
  const missing = models.missing_models ?? [];
  const optionalMissing = models.optional_missing_models ?? [];

  return (
    <section className="panel models-panel" aria-labelledby="models-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">OLLAMA RUNTIME</p>
          <h2 id="models-title">Models and connectivity</h2>
        </div>
        <Cpu aria-hidden="true" size={19} />
      </div>
      <div className="model-connection-row">
        <span className={`status-pill ${connection?.success ? "status-success" : "status-danger"}`}>
          <span className="connection-dot" aria-hidden="true" />
          {connection?.success ? "Connected" : "Unavailable"}
        </span>
        <a href={setupUrl} target="_blank" rel="noreferrer" className="setup-link">
          Ollama setup <ExternalLink aria-hidden="true" size={14} />
        </a>
        <button className="button button-quiet" onClick={() => void onCheck()} disabled={busy}>
          <RefreshCw aria-hidden="true" size={15} /> Rescan local models
        </button>
      </div>
      <p className="muted-note">{connection?.message ?? "Checking the local Ollama service."} Cortex only lists models already installed through Ollama on this PC.</p>
      <div className="model-list" aria-label="Installed models">
        {(models.installed_models ?? []).length ? (models.installed_models ?? []).map((installed) => (
          <span className="model-chip" key={installed}>{installed}</span>
        )) : <span className="empty-state">No installed models reported.</span>}
      </div>
      {missing.length > 0 && (
        <div className="model-missing">
          <strong>Required tags missing</strong>
          {missing.map((item) => <span key={item}>{item}</span>)}
        </div>
      )}
      {optionalMissing.length > 0 && (
        <div className="model-missing model-optional-missing">
          <strong>Optional features unavailable</strong>
          <span>Translation is enabled but its selected local model is unavailable.</span>
          {optionalMissing.map((item) => <span key={item}>{item}</span>)}
        </div>
      )}
      {progress && (
        <div className="model-progress" role="status" aria-label="Model operation progress" aria-live="polite" aria-busy={busy || undefined}>
          <div className="model-progress-heading">
            <span className="model-progress-model">
              {busy && <span className="loading-spinner model-progress-spinner" aria-hidden="true" />}
              {progress.model}
            </span>
            <span>{progress.percent === null ? progress.status : `${progress.percent}%`}</span>
          </div>
          <div className="progress-track"><span style={{ width: `${progress.percent ?? 8}%` }} /></div>
          <small>{progress.status}</small>
        </div>
      )}
    </section>
  );
}
