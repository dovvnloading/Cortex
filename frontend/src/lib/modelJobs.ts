import type { SSEEvent } from "../../../contracts/cortex-api";
import { formatModelSize } from "./localModels";

export type ModelJobProgress = { model: string; status: string; percent: number | null };

/**
 * What a download the person cancelled rejects with, so the form that started
 * it can say so plainly instead of reporting a failure.
 */
export class ModelJobCancelledError extends Error {
  constructor(message = "Download cancelled.") {
    super(message);
    this.name = "ModelJobCancelledError";
  }
}

const asNumber = (value: unknown): number | null => (
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null
);

function describeGGUFDownload(data: Record<string, unknown>): string {
  const completed = asNumber(data.completed);
  const total = asNumber(data.total);
  const sofar = completed === null ? null : (formatModelSize(completed) ?? "0 B");
  const whole = total === null ? null : formatModelSize(total);
  const bytes = sofar && whole ? ` (${sofar} of ${whole})` : sofar ? ` (${sofar})` : "";
  switch (data.message) {
    case "starting": return "Starting the download…";
    case "downloading": return `Downloading${bytes}`;
    case "retrying": return `Connection interrupted. Trying again${bytes}`;
    case "success": return "Download finished. Finishing up…";
    default: return `Downloading${bytes}`;
  }
}

/**
 * What a model job's progress event says to show, or `null` for an event that
 * is not progress (a state change or a terminal event).
 *
 * `label` names the job when the event does not: a download's events carry the
 * file name and byte counts but no `model`, and the label is what the person
 * asked for, so it is what the progress row keeps calling it.
 */
export function describeModelProgress(event: SSEEvent, label: string): ModelJobProgress | null {
  if (event.kind !== "progress") return null;
  const data = event.data ?? {};
  const model = typeof data.model === "string" ? data.model : label;
  const percent = asNumber(data.percent);
  if (event.phase === "gguf_download") {
    return { model, status: describeGGUFDownload(data), percent };
  }
  const status = typeof data.message === "string" ? data.message : event.phase ?? "Working";
  return { model, status, percent };
}
