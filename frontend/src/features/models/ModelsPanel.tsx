import { Download, ExternalLink, FolderOpen, RefreshCw } from "lucide-react";
import { useState } from "react";
import type {
  LlamaCppRuntimeStatus,
  ModelDownloadRequest,
  ModelResponse,
} from "../../../../contracts/cortex-api";
import { displayModelName } from "../../lib/localModels";
import { ModelInfoPanel } from "./ModelInfoPanel";

type Progress = {
  model: string;
  status: string;
  percent: number | null;
};

type GGUFControls = {
  directory: string;
  onDirectoryChange: (value: string) => void;
  onDownload: (request: ModelDownloadRequest) => Promise<void>;
  busy: boolean;
};

type Props = {
  models: ModelResponse;
  busy: boolean;
  progress: Progress | null;
  setupUrl: string;
  onCheck: () => Promise<void>;
  llamacppStatus: LlamaCppRuntimeStatus;
  gguf: GGUFControls;
};

export function ModelsPanel({ models, busy, progress, setupUrl, onCheck, llamacppStatus, gguf }: Props) {
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
          <small>{progress.status}</small>
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
      <GGUFDownloadForm onDownload={gguf.onDownload} busy={gguf.busy} />
    </div>
  );
}

function GGUFDownloadForm({ onDownload, busy }: { onDownload: (request: ModelDownloadRequest) => Promise<void>; busy: boolean }) {
  const [source, setSource] = useState<"huggingface" | "url">("huggingface");
  const [repoId, setRepoId] = useState("");
  const [filename, setFilename] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    const request: ModelDownloadRequest =
      source === "huggingface"
        ? { source, repo_id: repoId.trim(), filename: filename.trim() }
        : { source, url: url.trim() };
    try {
      await onDownload(request);
      setRepoId("");
      setFilename("");
      setUrl("");
    } catch {
      setError("The download did not complete. See the notification for details, or check the details above and try again.");
    }
  };

  const canSubmit = source === "huggingface" ? repoId.trim() && filename.trim() : url.trim();

  return (
    <div className="gguf-download-form">
      <div className="gguf-download-source-toggle" role="radiogroup" aria-label="Download source">
        <button
          type="button"
          className={`button button-quiet ${source === "huggingface" ? "icon-button-active" : ""}`}
          aria-pressed={source === "huggingface"}
          onClick={() => setSource("huggingface")}
        >
          Hugging Face
        </button>
        <button
          type="button"
          className={`button button-quiet ${source === "url" ? "icon-button-active" : ""}`}
          aria-pressed={source === "url"}
          onClick={() => setSource("url")}
        >
          Direct URL
        </button>
      </div>
      {source === "huggingface" ? (
        <div className="gguf-download-fields">
          <label className="field-label" htmlFor="gguf-repo-id">
            Repo id
            <input
              id="gguf-repo-id"
              value={repoId}
              placeholder="bartowski/some-model-GGUF"
              onChange={(event) => setRepoId(event.target.value)}
            />
          </label>
          <label className="field-label" htmlFor="gguf-filename">
            File name
            <input
              id="gguf-filename"
              value={filename}
              placeholder="some-model.Q4_K_M.gguf"
              onChange={(event) => setFilename(event.target.value)}
            />
          </label>
        </div>
      ) : (
        <label className="field-label" htmlFor="gguf-url">
          Direct .gguf URL
          <input
            id="gguf-url"
            value={url}
            placeholder="https://example.com/model.gguf"
            onChange={(event) => setUrl(event.target.value)}
          />
        </label>
      )}
      {error && <p className="field-error" role="alert">{error}</p>}
      <button
        className="button button-secondary"
        type="button"
        onClick={() => void submit()}
        disabled={!canSubmit || busy}
      >
        <Download aria-hidden="true" size={15} /> {busy ? "Downloading…" : "Download model"}
      </button>
    </div>
  );
}
