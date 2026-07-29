import { Download, ImagePlus, LoaderCircle, Square, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { ImageTransformPlan } from "../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../api/client";

type TransformKind = "grayscale" | "contrast" | "brightness";

type Props = {
  api: CortexApi;
  available: boolean;
  onSessionExpired?: () => void;
};

type ActiveTransform = {
  jobId: string;
  message: string;
};

const MAX_FILE_BYTES = 10 * 1024 * 1024;
const ACCEPTED_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export function ImageTransformPanel({ api, available, onSessionExpired }: Props) {
  const fileInputId = useId();
  const [expanded, setExpanded] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [resultUrl, setResultUrl] = useState<string | null>(null);
  const [resultMime, setResultMime] = useState("image/png");
  const [kind, setKind] = useState<TransformKind>("grayscale");
  const [factor, setFactor] = useState("1.5");
  const [active, setActive] = useState<ActiveTransform | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceUrlRef = useRef<string | null>(null);
  const resultUrlRef = useRef<string | null>(null);

  useEffect(() => () => {
    if (sourceUrlRef.current) URL.revokeObjectURL(sourceUrlRef.current);
    if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current);
  }, []);

  if (!available) return null;

  const replaceSourceUrl = (next: string | null) => {
    if (sourceUrlRef.current) URL.revokeObjectURL(sourceUrlRef.current);
    sourceUrlRef.current = next;
    setSourceUrl(next);
  };

  const replaceResultUrl = (next: string | null) => {
    if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current);
    resultUrlRef.current = next;
    setResultUrl(next);
  };

  const selectFile = (candidate: File | null) => {
    setError(null);
    replaceResultUrl(null);
    if (!candidate) {
      setFile(null);
      replaceSourceUrl(null);
      return;
    }
    if (!ACCEPTED_TYPES.has(candidate.type)) {
      setFile(null);
      replaceSourceUrl(null);
      setError("Choose a PNG, JPEG, or WebP image.");
      return;
    }
    if (candidate.size <= 0 || candidate.size > MAX_FILE_BYTES) {
      setFile(null);
      replaceSourceUrl(null);
      setError("Choose an image smaller than 10 MB.");
      return;
    }
    setFile(candidate);
    replaceSourceUrl(URL.createObjectURL(candidate));
  };

  const start = async () => {
    if (!file || active) return;
    setError(null);
    replaceResultUrl(null);
    setActive({ jobId: "staging", message: "Preparing image…" });
    try {
      const contentBase64 = await fileToBase64(file);
      const stage = await api.stageAttachment({
        request_id: requestId("image-stage"),
        content_base64: contentBase64,
      });
      const plan = buildPlan(stage.artifact_id, kind, factor);
      const accepted = await api.startRecipeImageTransform({
        request_id: requestId("image-transform"),
        source_artifact_id: stage.artifact_id,
        plan,
      });
      setActive({ jobId: accepted.job_id, message: "Starting image transformation…" });
      await watchTransform(api, accepted.job_id, (message) => {
        setActive({ jobId: accepted.job_id, message });
      });
      const completed = await api.executionStatus(accepted.job_id);
      if (completed.status !== "succeeded" || !completed.result) {
        throw new Error(completed.error ?? "The image transformation did not complete.");
      }
      const artifactId = completed.result.artifact_id;
      const mimeType = completed.result.mime_type;
      if (typeof artifactId !== "string" || typeof mimeType !== "string") {
        throw new Error("Cortex returned an incomplete image result.");
      }
      const download = await api.downloadExecutionArtifact(artifactId);
      replaceResultUrl(URL.createObjectURL(await download.blob()));
      setResultMime(mimeType);
      setActive(null);
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired?.();
        return;
      }
      setActive(null);
      setError(requestError instanceof ApiError ? requestError.detail : messageForError(requestError));
    }
  };

  const stop = async () => {
    if (!active || active.jobId === "staging") return;
    try {
      setActive((current) => current ? { ...current, message: "Stopping image transformation…" } : null);
      await api.cancelExecution(active.jobId);
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) onSessionExpired?.();
      else setError(requestError instanceof ApiError ? requestError.detail : "Could not stop the image transformation.");
    }
  };

  return (
    <section className={`image-transform ${expanded ? "image-transform-expanded" : ""}`} aria-label="Image transformation">
      <button
        className="image-transform-trigger"
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
      >
        <ImagePlus aria-hidden="true" size={16} />
        Transform image
      </button>
      {expanded && (
        <div className="image-transform-card">
          <div className="image-transform-heading">
            <div>
              <strong>Image transformation</strong>
              <span>Runs locally in a fixed, safe image worker.</span>
            </div>
            <button className="icon-button icon-button-small" type="button" aria-label="Close image transformation" onClick={() => setExpanded(false)} disabled={Boolean(active)}>
              <X aria-hidden="true" size={15} />
            </button>
          </div>
          <div className="image-transform-controls">
            <label className="field-label" htmlFor={fileInputId}>
              Image file
              <input
                id={fileInputId}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                disabled={Boolean(active)}
                onChange={(event) => selectFile(event.target.files?.[0] ?? null)}
              />
            </label>
            <label className="field-label" htmlFor="image-transform-kind">
              Change
              <select id="image-transform-kind" value={kind} disabled={Boolean(active)} onChange={(event) => setKind(event.target.value as TransformKind)}>
                <option value="grayscale">Make grayscale</option>
                <option value="contrast">Increase contrast</option>
                <option value="brightness">Adjust brightness</option>
              </select>
            </label>
            {kind !== "grayscale" && (
              <label className="field-label" htmlFor="image-transform-factor">
                Strength
                <input id="image-transform-factor" type="number" min="0" max="4" step="0.1" value={factor} disabled={Boolean(active)} onChange={(event) => setFactor(event.target.value)} />
              </label>
            )}
          </div>
          {sourceUrl && <div className="image-transform-preview"><img src={sourceUrl} alt="Selected image preview" /></div>}
          {active && (
            <div className="image-transform-status" role="status" aria-live="polite">
              <LoaderCircle aria-hidden="true" size={16} className="composer-control-spinner" />
              <span>{active.message}</span>
              {active.jobId !== "staging" && <button className="button button-secondary image-transform-stop" type="button" onClick={() => void stop()}><Square aria-hidden="true" size={12} fill="currentColor" />Stop</button>}
            </div>
          )}
          {error && <p className="field-error" role="alert">{error}</p>}
          {resultUrl && (
            <div className="image-transform-result">
              <img src={resultUrl} alt="Transformed image result" />
              <a className="button button-secondary" href={resultUrl} download={`cortex-result.${extensionForMime(resultMime)}`}>
                <Download aria-hidden="true" size={15} /> Download result
              </a>
            </div>
          )}
          <div className="image-transform-actions">
            <button className="button button-primary" type="button" onClick={() => void start()} disabled={!file || Boolean(active)}>
              {active && <LoaderCircle aria-hidden="true" size={15} className="composer-control-spinner" />}
              {active ? "Working…" : "Transform image"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function buildPlan(artifactId: string, kind: TransformKind, factor: string): ImageTransformPlan {
  const numericFactor = Number(factor);
  const safeFactor = Number.isFinite(numericFactor) && numericFactor >= 0 && numericFactor <= 4
    ? numericFactor
    : 1.5;
  const steps = kind === "grayscale"
    ? [{ op: "grayscale" as const }]
    : [{ op: kind, factor: safeFactor }];
  return {
    schema_version: "artifact.transform.v1",
    input_artifact_id: artifactId,
    steps,
    output_format: "png",
    strip_metadata: true,
  };
}

async function watchTransform(api: CortexApi, jobId: string, onStatus: (message: string) => void): Promise<void> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const status = await api.executionStatus(jobId);
    if (status.message) onStatus(status.message);
    if (status.status === "succeeded") return;
    if (status.status === "failed" || status.status === "cancelled") {
      throw new Error(status.error ?? "The image transformation did not complete.");
    }
    await delay(300);
  }
  throw new Error("The image transformation took too long. Try a smaller image.");
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Cortex could not read this image."));
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : "";
      const encoded = value.split(",", 2)[1];
      if (!encoded) reject(new Error("Cortex could not read this image."));
      else resolve(encoded);
    };
    reader.readAsDataURL(file);
  });
}

function requestId(prefix: string): string {
  const value = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 128);
}

function extensionForMime(mime: string): string {
  return mime === "image/jpeg" ? "jpg" : mime === "image/webp" ? "webp" : "png";
}

function messageForError(error: unknown): string {
  return error instanceof Error ? error.message : "The image transformation could not be completed.";
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
