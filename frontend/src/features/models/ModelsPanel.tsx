import { ExternalLink, FolderOpen, RefreshCw, X } from "lucide-react";
import type {
  LlamaCppRuntimeStatus,
  ModelDownloadRequest,
  ModelResponse,
} from "../../../../contracts/cortex-api";
import { displayModelName } from "../../lib/localModels";
import { GGUFDownloadForm, type ListGGUFFiles } from "./GGUFDownloadForm";
import { ModelInfoPanel } from "./ModelInfoPanel";

type Progress = {
  model: string;
  status: string;
  percent: number | null;
  /** The person asked for this operation to stop and it has not yet. */
  cancelling?: boolean;
};

type GGUFControls = {
  directory: string;
  directoryDirty: boolean;
  onDirectoryChange: (value: string) => void;
  onDownload: (request: ModelDownloadRequest) => Promise<void>;
  busy: boolean;
  /** Lists a Hugging Face repository's files; without it the form only takes a typed file name. */
  onListFiles?: ListGGUFFiles;
};

type Props = {
  models: ModelResponse;
  busy: boolean;
  progress: Progress | null;
  /** Stops the operation being shown; without it the progress row has no Cancel. */
  onCancel?: () => void;
  setupUrl: string;
  onCheck: () => Promise<void>;
  llamacppStatus: LlamaCppRuntimeStatus;
  gguf: GGUFControls;
};

export function ModelsPanel({ models, busy, progress, onCancel, setupUrl, onCheck, llamacppStatus, gguf }: Props) {
  const connection = models.connection;
  const missing = models.missing_models ?? [];
  const optionalMissing = models.optional_missing_models ?? [];

  return (
    <section className="panel models-panel" aria-labelledby="models-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">LOCAL MODELS</p>
          <h2 id="models-title">Models and connectivity</h2>
        </div>
      </div>
      <div className="model-connection-row">
        <span className={`status-pill ${connection?.success ? "status-success" : "status-danger"}`}>
          <span className="connection-dot" aria-hidden="true" />
          {connection?.success ? "Ollama connected" : "Ollama unavailable"}
        </span>
        <a href={setupUrl} target="_blank" rel="noreferrer" className="setup-link">
          Ollama setup <ExternalLink aria-hidden="true" size={14} />
        </a>
        <button className="button button-quiet" onClick={() => void onCheck()} disabled={busy}>
          <RefreshCw aria-hidden="true" size={15} /> Rescan local models
        </button>
      </div>
      <p className="muted-note">
        {connection?.message ?? "Checking the local Ollama service."} Cortex lists models installed through
        Ollama and any .gguf files in your local models folder.
      </p>
      <div className="model-list" aria-label="Installed models">
        {(models.models ?? []).length ? (models.models ?? []).map((model) => (
          <ModelInfoPanel model={model} key={model.name} />
        )) : (models.installed_models ?? []).length ? (models.installed_models ?? []).map((installed) => (
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
          <div className="model-progress-footer">
            <small>{progress.status}</small>
            {busy && onCancel && (
              <button
                className="button button-quiet model-progress-cancel"
                type="button"
                onClick={onCancel}
                disabled={progress.cancelling}
                aria-label={`Cancel ${progress.model}`}
              >
                <X aria-hidden="true" size={14} /> {progress.cancelling ? "Cancelling…" : "Cancel"}
              </button>
            )}
          </div>
        </div>
      )}
      <GGUFRuntimeSection llamacppStatus={llamacppStatus} gguf={gguf} />
    </section>
  );
}

function GGUFRuntimeSection({ llamacppStatus, gguf }: { llamacppStatus: LlamaCppRuntimeStatus; gguf: GGUFControls }) {
  return (
    <div className="gguf-runtime">
      <div className="section-heading">
        <p className="eyebrow">GGUF MODELS</p>
        <h3>Local models folder</h3>
      </div>
      <p className="muted-note">
        Drop a .gguf file into this folder, or download one below. Cortex downloads and runs the local model
        runtime automatically the first time you use a GGUF model{llamacppStatus.last_error ? ` (${llamacppStatus.last_error})` : "."}
      </p>
      {llamacppStatus.active_backend && (
        <p className="gguf-runtime-backend">
          Local runtime: <strong>{llamacppStatus.active_backend === "vulkan" ? "GPU (Vulkan)" : "CPU"}</strong>
          {llamacppStatus.state === "ready" && llamacppStatus.loaded_model
            ? ` — currently running ${displayModelName(llamacppStatus.loaded_model)}`
            : llamacppStatus.state === "starting" || llamacppStatus.state === "downloading_binary"
              ? " — starting…"
              : ""}
        </p>
      )}
      <div className="gguf-runtime-directory">
        <FolderOpen aria-hidden="true" size={15} />
        <input
          type="text"
          aria-label="GGUF models folder"
          value={gguf.directory}
          placeholder={llamacppStatus.models_directory || "Default models folder"}
          onChange={(event) => gguf.onDirectoryChange(event.target.value)}
        />
      </div>
      {gguf.directory && gguf.directory !== llamacppStatus.models_directory && llamacppStatus.models_directory && (
        <small className="gguf-runtime-directory-hint">
          Cortex will look in: {llamacppStatus.models_directory}
          {gguf.directory.toLowerCase().endsWith(".gguf") ? " (the folder containing the file you entered)" : ""}
        </small>
      )}
      {!llamacppStatus.models_directory_exists && (
        <p className="field-error" role="alert">
          This folder does not exist yet ({llamacppStatus.models_directory || "not set"}). Create it, point at an
          existing folder, or leave this blank to use the default -- Cortex will not see any models here until the
          folder exists. Tip: point this at the <em>folder</em> a .gguf file is in, not the file itself.
        </p>
      )}
      {gguf.directoryDirty && (
        <p className="muted-note" role="status">
          Save the folder setting before downloading a model. Downloads use the saved folder.
        </p>
      )}
      <GGUFDownloadForm
        onDownload={gguf.onDownload}
        busy={gguf.busy}
        directoryDirty={gguf.directoryDirty}
        onListFiles={gguf.onListFiles}
      />
    </div>
  );
}
