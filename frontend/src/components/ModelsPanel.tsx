import { ExternalLink, RefreshCw, Download, Cpu } from "lucide-react";
import { useState } from "react";
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
  onPull: (model: string) => Promise<void>;
};

export function ModelsPanel({ models, busy, progress, setupUrl, onCheck, onPull }: Props) {
  const [model, setModel] = useState("");
  const connection = models.connection;
  const missing = models.missing_models ?? [];

  const submit = () => {
    const value = model.trim();
    if (!value) return;
    void onPull(value).then(() => setModel(""));
  };

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
          <RefreshCw aria-hidden="true" size={15} /> Check now
        </button>
      </div>
      <p className="muted-note">{connection?.message ?? "Checking the local Ollama service."}</p>
      <div className="model-list" aria-label="Installed models">
        {(models.installed_models ?? []).length ? (models.installed_models ?? []).map((installed) => (
          <span className="model-chip" key={installed}>{installed}</span>
        )) : <span className="empty-state">No installed models reported.</span>}
      </div>
      {missing.length > 0 && (
        <div className="model-missing">
          <strong>Required tags missing</strong>
          {missing.map((item) => <button key={item} className="button button-secondary" onClick={() => void onPull(item)} disabled={busy}><Download aria-hidden="true" size={14} /> Pull {item}</button>)}
        </div>
      )}
      <div className="model-pull-form">
        <label className="field-label" htmlFor="model-tag">Pull an exact model tag
          <input id="model-tag" value={model} onChange={(event) => setModel(event.target.value)} placeholder="nemotron-3-nano:4b" maxLength={200} />
        </label>
        <button className="button button-primary" onClick={submit} disabled={busy || !model.trim()}><Download aria-hidden="true" size={15} /> Pull model</button>
      </div>
      {progress && (
        <div className="model-progress" role="status" aria-live="polite">
          <div className="model-progress-heading"><span>{progress.model}</span><span>{progress.percent === null ? progress.status : `${progress.percent}%`}</span></div>
          <div className="progress-track"><span style={{ width: `${progress.percent ?? 8}%` }} /></div>
          <small>{progress.status}</small>
        </div>
      )}
    </section>
  );
}
