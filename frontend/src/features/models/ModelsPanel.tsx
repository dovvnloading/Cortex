import { ExternalLink, FolderOpen, PowerOff, RefreshCw, X } from "lucide-react";
import { useState } from "react";
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

/** How the local runtime uses memory, and the advanced options it is launched with. */
export type RuntimeControls = {
  /** Minutes without a request after which the loaded model is released; 0 keeps it loaded. */
  idleUnloadMinutes: number;
  onIdleUnloadMinutesChange: (minutes: number) => void;
  /** Advanced runtime options as words, e.g. `["-ctk", "q8_0", "-t", "8"]`. */
  extraArgs: readonly string[];
  onExtraArgsChange: (args: string[]) => void;
  /** Absent when this build cannot unload a model; the button is then left out. */
  onUnload?: () => Promise<void>;
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
  runtime?: RuntimeControls;
};

/**
 * What the status can honestly say about which processor is doing the work.
 * The build that launched is not the same as the GPU being used: with automatic
 * layer offload only some (or none) of the model may be on it, so the layer
 * counts the runtime reported win over the name of the build.
 */
function describeRuntimeBackend(status: LlamaCppRuntimeStatus): string {
  if (status.active_backend !== "vulkan") return "CPU";
  const { gpu_layers_offloaded: onGpu, gpu_layers_total: total } = status;
  if (status.state !== "ready") return "GPU (Vulkan)";
  if (typeof onGpu !== "number" || typeof total !== "number") {
    return "GPU (Vulkan) — how much of the model is on the GPU was not reported";
  }
  if (onGpu === 0) return `CPU — the GPU build is running, but none of the ${total} layers are on the GPU`;
  return `GPU (Vulkan) · ${onGpu}/${total} layers on the GPU`;
}

export function ModelsPanel({ models, busy, progress, onCancel, setupUrl, onCheck, llamacppStatus, gguf, runtime }: Props) {
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
      <GGUFRuntimeSection llamacppStatus={llamacppStatus} gguf={gguf} runtime={runtime} />
    </section>
  );
}

function GGUFRuntimeSection({ llamacppStatus, gguf, runtime }: { llamacppStatus: LlamaCppRuntimeStatus; gguf: GGUFControls; runtime?: RuntimeControls }) {
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
          Local runtime: <strong>{describeRuntimeBackend(llamacppStatus)}</strong>
          {llamacppStatus.state === "ready" && llamacppStatus.loaded_model
            ? ` — currently running ${displayModelName(llamacppStatus.loaded_model)}`
            : llamacppStatus.state === "starting" || llamacppStatus.state === "downloading_binary"
              ? " — starting…"
              : ""}
        </p>
      )}
      {llamacppStatus.backend_note && <p className="muted-note" role="status">{llamacppStatus.backend_note}</p>}
      {llamacppStatus.context_note && <p className="muted-note" role="status">{llamacppStatus.context_note}</p>}
      {runtime && <RuntimeMemoryControls status={llamacppStatus} runtime={runtime} />}
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

const MAX_IDLE_UNLOAD_MINUTES = 1440;

function RuntimeMemoryControls({ status, runtime }: { status: LlamaCppRuntimeStatus; runtime: RuntimeControls }) {
  const [unloading, setUnloading] = useState(false);
  const [idleText, setIdleText] = useState(String(runtime.idleUnloadMinutes));
  const [optionsText, setOptionsText] = useState(runtime.extraArgs.join(" "));

  // Follow a value that changed elsewhere (a save, a reset) without fighting
  // what is being typed: text that already means the same thing is left alone.
  // Adjusted while rendering, as the settings panel does, so the field never
  // shows a stale value for a frame.
  const [seenMinutes, setSeenMinutes] = useState(runtime.idleUnloadMinutes);
  if (seenMinutes !== runtime.idleUnloadMinutes) {
    setSeenMinutes(runtime.idleUnloadMinutes);
    if (parseIdleMinutes(idleText) !== runtime.idleUnloadMinutes) setIdleText(String(runtime.idleUnloadMinutes));
  }
  const argsKey = runtime.extraArgs.join(" ");
  const [seenArgs, setSeenArgs] = useState(argsKey);
  if (seenArgs !== argsKey) {
    setSeenArgs(argsKey);
    if (splitWords(optionsText).join(" ") !== argsKey) setOptionsText(argsKey);
  }

  const unload = async () => {
    if (!runtime.onUnload) return;
    setUnloading(true);
    try {
      await runtime.onUnload();
    } finally {
      setUnloading(false);
    }
  };

  const loaded = status.state === "ready" && Boolean(status.loaded_model);
  return (
    <div className="gguf-runtime-memory">
      {runtime.onUnload && (
        <div className="gguf-runtime-unload">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void unload()}
            disabled={!loaded || unloading}
          >
            <PowerOff aria-hidden="true" size={15} /> {unloading ? "Unloading…" : "Unload model"}
          </button>
          <small className="gguf-runtime-directory-hint">
            {loaded
              ? "Frees the memory the model is using. It loads again when you send a message."
              : "No local model is loaded right now."}
          </small>
        </div>
      )}
      <div className="gguf-runtime-field">
        <label className="field-label" htmlFor="gguf-idle-unload">
          Unload an unused model after (minutes)
          <input
            id="gguf-idle-unload"
            type="number"
            inputMode="numeric"
            min={0}
            max={MAX_IDLE_UNLOAD_MINUTES}
            step={1}
            value={idleText}
            aria-describedby="gguf-idle-unload-hint"
            onChange={(event) => {
              setIdleText(event.target.value);
              const minutes = parseIdleMinutes(event.target.value);
              if (minutes !== null) runtime.onIdleUnloadMinutesChange(minutes);
            }}
          />
        </label>
        <small id="gguf-idle-unload-hint" className="gguf-runtime-directory-hint">
          0 keeps the model loaded until Cortex closes. A response in progress is never interrupted.
        </small>
      </div>
      <div className="gguf-runtime-field">
        <label className="field-label" htmlFor="gguf-extra-args">
          Advanced runtime options
          <input
            id="gguf-extra-args"
            type="text"
            spellCheck={false}
            autoComplete="off"
            value={optionsText}
            placeholder="-ctk q8_0 -ctv q8_0 -fa on -t 8"
            aria-describedby="gguf-extra-args-hint"
            onChange={(event) => {
              setOptionsText(event.target.value);
              runtime.onExtraArgsChange(splitWords(event.target.value));
            }}
          />
        </label>
        <small id="gguf-extra-args-hint" className="gguf-runtime-directory-hint">
          KV-cache types (-ctk, -ctv), flash attention (-fa) and thread counts (-t, -tb) only. Applied the next time the
          model loads; Cortex keeps setting the model, context window and address itself.
        </small>
      </div>
    </div>
  );
}

/** A whole number of minutes from 0 to a day, or `null` while the text is not one. */
function parseIdleMinutes(text: string): number | null {
  if (!/^\d{1,4}$/.test(text.trim())) return null;
  const minutes = Number(text.trim());
  return minutes <= MAX_IDLE_UNLOAD_MINUTES ? minutes : null;
}

function splitWords(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
}
