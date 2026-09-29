import { Download, ExternalLink, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { HuggingFaceFileListResponse, ModelDownloadRequest } from "../../../../contracts/cortex-api";
import { ApiError, describeApiError, isAbortedError } from "../../api/client";
import { groupGGUFFiles, HUGGING_FACE_REPO_ID, type GGUFFileChoice } from "../../lib/huggingFaceFiles";
import { formatModelSize } from "../../lib/localModels";
import { ModelJobCancelledError } from "../../lib/modelJobs";

/** Lists the `.gguf` files of a Hugging Face repository; `signal` aborts a listing nobody is waiting for. */
export type ListGGUFFiles = (repoId: string, signal: AbortSignal) => Promise<HuggingFaceFileListResponse>;

type Props = {
  onDownload: (request: ModelDownloadRequest) => Promise<void>;
  busy: boolean;
  directoryDirty: boolean;
  /** Without it the form has no file browser and asks for the file name, as it always did. */
  onListFiles?: ListGGUFFiles;
};

type Listing =
  | { status: "idle" }
  | { status: "loading"; repo: string }
  | { status: "ready"; repo: string; choices: GGUFFileChoice[] }
  | { status: "failed"; repo: string; message: string; code: string | null };

/** A download that stopped, for the form to say why beside the button. */
type Outcome = { message: string; cancelled: boolean };

/** The failures where opening the repository page is the next step: accept its terms, or check the name. */
const OPEN_REPOSITORY_CODES = new Set(["gated", "not_found"]);

function describeChoice(choice: GGUFFileChoice): string {
  if (!choice.complete) return `${choice.parts}-part model, missing a part in this repository`;
  const size = choice.size === null ? null : formatModelSize(choice.size);
  const parts = choice.parts > 1 ? `${choice.parts} parts` : null;
  return [parts, size].filter(Boolean).join(" · ");
}

export function GGUFDownloadForm({ onDownload, busy, directoryDirty, onListFiles }: Props) {
  const [source, setSource] = useState<"huggingface" | "url">("huggingface");
  const [repoId, setRepoId] = useState("");
  const [filename, setFilename] = useState("");
  // True while the file name is one picked from the list rather than typed, so
  // a different repository does not keep a file name that belongs to the old one.
  const [picked, setPicked] = useState(false);
  const [url, setUrl] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [listing, setListing] = useState<Listing>({ status: "idle" });
  const [manualOpen, setManualOpen] = useState(false);
  const listRequest = useRef<AbortController | null>(null);

  useEffect(() => () => listRequest.current?.abort(), []);

  const repo = repoId.trim();
  const canBrowse = onListFiles !== undefined && HUGGING_FACE_REPO_ID.test(repo) && listing.status !== "loading";

  const changeRepo = (value: string) => {
    setRepoId(value);
    // Whatever was listed, or is being listed, belongs to the old repository.
    listRequest.current?.abort();
    setListing((current) => (current.status === "idle" ? current : { status: "idle" }));
    if (picked) {
      setFilename("");
      setPicked(false);
    }
  };

  const browse = async () => {
    if (!onListFiles || !HUGGING_FACE_REPO_ID.test(repo)) return;
    listRequest.current?.abort();
    const request = new AbortController();
    listRequest.current = request;
    setListing({ status: "loading", repo });
    try {
      const response = await onListFiles(repo, request.signal);
      if (request.signal.aborted) return;
      // An older backend lists names only.
      const entries = response.entries ?? (response.files ?? []).map((path) => ({ path }));
      setListing({ status: "ready", repo, choices: groupGGUFFiles(entries) });
    } catch (error) {
      if (request.signal.aborted || isAbortedError(error)) return;
      setListing({
        status: "failed",
        repo,
        message: describeApiError(error, "Could not list the files in this repository."),
        code: error instanceof ApiError ? error.code : null,
      });
      // A failed listing must never stand between the person and the download.
      setManualOpen(true);
    }
  };

  const submit = async () => {
    setOutcome(null);
    const request: ModelDownloadRequest =
      source === "huggingface"
        ? { source, repo_id: repo, filename: filename.trim() }
        : { source, url: url.trim() };
    try {
      await onDownload(request);
      setRepoId("");
      setFilename("");
      setPicked(false);
      setUrl("");
      setListing({ status: "idle" });
    } catch (caught) {
      const message = caught instanceof Error && caught.message
        ? caught.message
        : "The download did not complete. Check the details above and try again.";
      setOutcome({ message, cancelled: caught instanceof ModelJobCancelledError });
    }
  };

  const canSubmit = source === "huggingface" ? repo && filename.trim() : url.trim();
  // The backend fetches every part of a split model from any one of its names.
  const pickedParts = listing.status === "ready"
    ? listing.choices.find((choice) => choice.path === filename.trim())?.parts ?? 1
    : 1;

  const fileNameField = (
    <label className="field-label" htmlFor="gguf-filename">
      File name
      <input
        id="gguf-filename"
        value={filename}
        placeholder="some-model.Q4_K_M.gguf"
        onChange={(event) => {
          setFilename(event.target.value);
          setPicked(false);
        }}
      />
    </label>
  );

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
          <div className="gguf-repo-row">
            <label className="field-label" htmlFor="gguf-repo-id">
              Repo id
              <input
                id="gguf-repo-id"
                value={repoId}
                placeholder="bartowski/some-model-GGUF"
                onChange={(event) => changeRepo(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && canBrowse) {
                    event.preventDefault();
                    void browse();
                  }
                }}
              />
            </label>
            {onListFiles && (
              <button className="button button-secondary" type="button" onClick={() => void browse()} disabled={!canBrowse}>
                <Search aria-hidden="true" size={15} /> {listing.status === "failed" ? "Try again" : "Browse files"}
              </button>
            )}
          </div>
          {listing.status === "loading" && (
            <p className="muted-note" role="status">Looking up the .gguf files in {listing.repo}…</p>
          )}
          {listing.status === "failed" && (
            <div className="gguf-listing-problem" role="alert">
              <p className="field-error">{listing.message}</p>
              {listing.code !== null && OPEN_REPOSITORY_CODES.has(listing.code) && (
                <a className="setup-link" href={`https://huggingface.co/${listing.repo}`} target="_blank" rel="noreferrer">
                  Open {listing.repo} on Hugging Face <ExternalLink aria-hidden="true" size={14} />
                </a>
              )}
              <p className="muted-note">You can still type the file name yourself.</p>
            </div>
          )}
          {listing.status === "ready" && (
            listing.choices.length === 0 ? (
              <p className="muted-note" role="status">
                No .gguf files were found in {listing.repo}. Check the repository id, or type a file name below.
              </p>
            ) : (
              <fieldset className="gguf-file-picker">
                <legend>Files in {listing.repo}</legend>
                {Array.from(new Set(listing.choices.map((choice) => choice.directory))).map((directory) => (
                  <div role="group" aria-label={directory || "Top level of the repository"} key={directory}>
                    {directory && <div className="gguf-file-folder">{directory}/</div>}
                    {listing.choices.filter((choice) => choice.directory === directory).map((choice) => (
                      <label className={`gguf-file-option${choice.complete ? "" : " gguf-file-option-disabled"}`} key={choice.path}>
                        <input
                          type="radio"
                          name="gguf-file"
                          value={choice.path}
                          checked={filename === choice.path}
                          disabled={!choice.complete}
                          onChange={() => {
                            setFilename(choice.path);
                            setPicked(true);
                          }}
                        />
                        <span className="gguf-file-name">{choice.label}</span>
                        <span className="gguf-file-meta">{describeChoice(choice)}</span>
                      </label>
                    ))}
                  </div>
                ))}
              </fieldset>
            )
          )}
          {onListFiles ? (
            <details
              className="gguf-manual"
              open={manualOpen}
              onToggle={(event) => setManualOpen(event.currentTarget.open)}
            >
              <summary>Advanced: type a file name</summary>
              {fileNameField}
            </details>
          ) : fileNameField}
          {onListFiles && filename.trim() && (
            <p className="gguf-file-selected">
              {pickedParts > 1
                ? <>Will download all {pickedParts} parts, starting with <strong>{filename.trim()}</strong></>
                : <>Will download <strong>{filename.trim()}</strong></>}
            </p>
          )}
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
      {outcome && (
        outcome.cancelled
          ? <p className="muted-note" role="status">{outcome.message}</p>
          : <p className="field-error" role="alert">{outcome.message}</p>
      )}
      <button
        className="button button-secondary"
        type="button"
        onClick={() => void submit()}
        disabled={!canSubmit || busy || directoryDirty}
      >
        <Download aria-hidden="true" size={15} /> {busy ? "Downloading…" : "Download model"}
      </button>
    </div>
  );
}
