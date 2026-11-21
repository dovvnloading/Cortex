import { useEffect, useState } from "react";
import type { AgentRunResponse } from "../../../../contracts/cortex-api";
import { normalizeApiBaseUrl } from "../../api/baseUrl";
import type { AgentApi } from "./agentApi";

// The current run-list contract does not include goals. Display one when a
// future API supplies it, but never substitute a path, command, or file text.
type RunSummary = Pick<AgentRunResponse, "id" | "project_root" | "status" | "created"> & {
  goal?: string;
};

type RunListState =
  | { phase: "loading" | "error"; runs: [] }
  | { phase: "ready"; runs: RunSummary[] };

type RunAction = "resume" | "discard";

export type RunHistoryProps = {
  onOpen: (runId: string) => void;
  /** Optional transport for embedding or testing; defaults to browser fetch. */
  fetcher?: typeof fetch;
  /** The mounted Agent page supplies its authenticated API facade. */
  api?: AgentApi;
  onDiscarded?: (runId: string) => void;
  refreshKey?: string | number;
};

const API_BASE_URL = normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL, import.meta.env.PROD);
const DISCARDABLE = new Set(["completed", "failed", "cancelled", "interrupted"]);
const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  waiting_approval: "Waiting for approval",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

function isRunSummary(value: unknown): value is RunSummary {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const run = value as Record<string, unknown>;
  return typeof run.id === "string" && run.id.length > 0
    && typeof run.project_root === "string"
    && typeof run.status === "string"
    && typeof run.created === "string"
    && (run.goal === undefined || typeof run.goal === "string");
}

function parseRuns(value: unknown): RunSummary[] {
  if (value === null || typeof value !== "object" || !("runs" in value)) {
    throw new Error("Invalid run list");
  }
  const runs = value.runs;
  if (!Array.isArray(runs) || !runs.every(isRunSummary)) {
    throw new Error("Invalid run list");
  }
  return [...runs].sort((a, b) => {
    const aTime = Date.parse(a.created);
    const bTime = Date.parse(b.created);
    return (Number.isNaN(bTime) ? -Infinity : bTime) - (Number.isNaN(aTime) ? -Infinity : aTime);
  });
}

function projectFolder(projectRoot: string): string {
  return projectRoot.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "Project folder unavailable";
}

function runDate(created: string): string {
  const date = new Date(created);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString();
}

function requestHeaders(): Headers {
  const headers = new Headers({ Accept: "application/json" });
  try {
    const token = window.sessionStorage.getItem("cortex.session.token");
    if (token) headers.set("Authorization", `Bearer ${token}`);
  } catch {
    // Storage can be unavailable; an unauthenticated request fails visibly.
  }
  return headers;
}

/** Metadata-only agent history; it does not fetch source, changesets, or output. */
export function RunHistory({ onOpen, fetcher, api, onDiscarded, refreshKey }: RunHistoryProps) {
  const [state, setState] = useState<RunListState>({ phase: "loading", runs: [] });
  const [refresh, setRefresh] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        let payload: unknown;
        if (api) {
          payload = await api.list();
        } else {
          const request = fetcher ?? window.fetch.bind(window);
          const response = await request(`${API_BASE_URL}/agent/runs`, {
            method: "GET",
            headers: requestHeaders(),
            signal: controller.signal,
          });
          if (!response.ok) throw new Error("Run list unavailable");
          payload = await response.json() as unknown;
        }
        const runs = parseRuns(payload);
        if (!controller.signal.aborted) setState({ phase: "ready", runs });
      } catch {
        if (!controller.signal.aborted) setState({ phase: "error", runs: [] });
      }
    })();
    return () => controller.abort();
  }, [fetcher, refresh, api, refreshKey]);

  const act = async (run: RunSummary, action: RunAction) => {
    if (busyId !== null) return;
    if (action === "resume" && run.status !== "interrupted") return;
    if (action === "discard" && !DISCARDABLE.has(run.status)) return;

    const prompt = action === "resume"
      ? "Resume this interrupted agent run? It may continue changing its work copy."
      : "Delete this agent run? The work copy will be removed. Any previously accepted project edits remain until explicitly undone.";
    if (!window.confirm(prompt)) return;

    setBusyId(run.id);
    setActionError(null);
    setNotice(null);
    try {
      let payload: unknown;
      if (api) {
        payload = action === "discard" ? await api.discard(run.id, true) : await api.resume(run.id);
      } else {
        const request = fetcher ?? window.fetch.bind(window);
        const response = await request(`${API_BASE_URL}/agent/runs/${encodeURIComponent(run.id)}/${action}`, {
          method: "POST",
          headers: requestHeaders(),
        });
        if (!response.ok) throw new Error("Run action unavailable");
        payload = await response.json() as unknown;
      }
      if (action === "discard") {
        if (payload === null || typeof payload !== "object" || !("discarded" in payload) || payload.discarded !== true) {
          throw new Error("Discard not acknowledged");
        }
        setState((current) => current.phase === "ready"
          ? { ...current, runs: current.runs.filter((item) => item.id !== run.id) }
          : current);
        setNotice("The run's work copy was removed.");
        onDiscarded?.(run.id);
      } else {
        if (!isRunSummary(payload) || payload.id !== run.id) throw new Error("Invalid resume response");
        setState((current) => current.phase === "ready"
          ? { ...current, runs: current.runs.map((item) => item.id === run.id
            ? { ...item, status: payload.status }
            : item) }
          : current);
        setNotice("Run resumed.");
        if (api) onOpen(run.id);
      }
    } catch {
      setActionError(action === "resume"
        ? "Could not resume the run. Try again."
        : "Could not delete the run. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section aria-label="Agent run history" style={{ border: "1px solid var(--line)", borderRadius: "var(--radius-md)", padding: 16 }}>
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: "1.15rem" }}>Agent run history</h2>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => { setActionError(null); setNotice(null); setState({ phase: "loading", runs: [] }); setRefresh((value) => value + 1); }}
          disabled={busyId !== null || state.phase === "loading"}
        >
          Refresh
        </button>
      </header>

      {state.phase === "loading" && <p role="status">Loading agent runs…</p>}
      {state.phase === "error" && (
        <div>
          <p role="alert">Could not load agent runs.</p>
          <button className="button button-secondary" type="button" onClick={() => { setState({ phase: "loading", runs: [] }); setRefresh((value) => value + 1); }}>Retry</button>
        </div>
      )}
      {state.phase === "ready" && state.runs.length === 0 && <p>No agent runs yet.</p>}
      {actionError && <p role="alert">{actionError}</p>}
      {notice && <p role="status">{notice}</p>}

      {state.phase === "ready" && state.runs.length > 0 && (
        <ul aria-label="Agent runs" style={{ listStyle: "none", padding: 0, margin: "16px 0 0", display: "grid", gap: 12 }}>
          {state.runs.map((run) => {
            const goal = run.goal?.trim() || "Goal unavailable";
            const status = STATUS_LABELS[run.status] ?? "Unknown status";
            return (
              <li key={run.id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, borderBottom: "1px solid var(--line)", paddingBottom: 12 }}>
                <div style={{ flex: "1 1 220px", minWidth: 0 }}>
                  <button
                    type="button"
                    className="button button-quiet"
                    style={{ display: "block", textAlign: "left", padding: "6px 0", height: "auto", maxWidth: "100%", whiteSpace: "normal", overflowWrap: "anywhere" }}
                    onClick={() => onOpen(run.id)}
                    aria-label={`Open run: ${goal}`}
                  >
                    <strong>{goal}</strong>
                  </button>
                  <div style={{ color: "var(--text-muted)", fontSize: "0.84rem", display: "flex", flexWrap: "wrap", gap: 8 }}>
                    <span>{status}</span>
                    <span>{projectFolder(run.project_root)}</span>
                    <time dateTime={run.created}>{runDate(run.created)}</time>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {run.status === "interrupted" && (
                    <button
                      type="button"
                      className="button button-secondary"
                      disabled={busyId !== null}
                      onClick={() => void act(run, "resume")}
                      aria-label={`Resume run: ${goal}`}
                    >Resume</button>
                  )}
                  <button
                    type="button"
                    className="button button-secondary"
                    disabled={busyId !== null || !DISCARDABLE.has(run.status)}
                    onClick={() => void act(run, "discard")}
                    aria-label={`Delete run: ${goal}`}
                  >Delete</button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
