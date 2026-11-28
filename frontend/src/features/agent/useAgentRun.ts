import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentRunResponse, AgentSSEEvent, DecideAgentApproval, StartAgentRun } from "../../../../contracts/cortex-api";
import { ApiError, describeApiError } from "../../api/client";
import { cloudModelProvider } from "../../lib/localModels";
import type { ApprovalRequest } from "./ApprovalCard";
import type { AgentApi } from "./agentApi";

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
const KNOWN_STATUSES = new Set(["pending", "running", "waiting_approval", ...TERMINAL]);
const APPROVAL_CLASSES = new Set<ApprovalRequest["cls"]>([
  "inspect", "build_test", "mutate_workcopy", "network", "dangerous", "ask",
]);

export type PendingAgentApproval = { seq: number; request: ApprovalRequest };

/** The backend's one-time capability probe result for the run's model. */
export type ModelCheck = { recommended: boolean; failedChecks: string[] };

function objectData(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function stringField(data: Record<string, unknown>, key: string): string | null {
  return typeof data[key] === "string" ? data[key] as string : null;
}

/** What a tool approval will do, as plain text for the details view. */
function fileChangeDetails(request: Record<string, unknown>, tool: string, path: string | null): string {
  const lines = ["Requested tool: " + tool];
  const url = stringField(request, "url");
  if (url) lines.push("Destination URL: " + url, "This request uses the internet.");
  if (path) lines.push("Project file: " + path);
  const source = stringField(request, "source");
  const destination = stringField(request, "destination");
  if (source) lines.push("Move from: " + source);
  if (destination) lines.push("Move to: " + destination);
  const preview = objectData(request.preview);
  if (preview) {
    const before = stringField(preview, "old");
    const after = stringField(preview, "new");
    const content = stringField(preview, "content");
    if (before !== null && after !== null) {
      lines.push("", preview.replace_all === true ? "Replace every occurrence of:" : "Replace this text:", before,
        "", "With:", after);
    } else if (content !== null) {
      lines.push("", "File content:", content);
    }
    if (preview.truncated === true) lines.push("", "(Shortened here. The review shows the full change after it is staged.)");
  }
  return lines.join("\n");
}

/** Unrecognized approvals are shown conservatively as unsandboxed, never as safe. */
function parseApproval(value: unknown): ApprovalRequest | null {
  const request = objectData(value);
  if (!request) return null;
  const cls = stringField(request, "cls");
  const tool = stringField(request, "tool") ?? "a project action";
  const path = stringField(request, "path");
  const command = stringField(request, "command");
  const url = stringField(request, "url");
  // A web fetch is approved for one exact URL; without it there is nothing
  // the user could review, so the request is not offered for approval.
  if (tool === "web_fetch" && !url?.trim()) return null;
  const summary = stringField(request, "plain_text")
    ?? stringField(request, "summary")
    ?? "The agent requests permission for " + tool + ".";
  return {
    command: command ?? fileChangeDetails(request, tool, path),
    cls: cls && APPROVAL_CLASSES.has(cls as ApprovalRequest["cls"])
      ? cls as ApprovalRequest["cls"] : "ask",
    plain_text: url ? summary + " URL: " + url : summary,
    // The shell tool reports its launcher as "level". Never assert that a
    // command is sandboxed, or that no process runs, unless that was supplied.
    sandbox_level: command !== null
      ? (request.level === "appcontainer" ? "appcontainer" : "unsandboxed")
      : (request.scope === "workcopy" ? "workcopy" : "unsandboxed"),
    network: request.network === true || url !== null,
  };
}

function latestApproval(events: readonly AgentSSEEvent[]): PendingAgentApproval | null {
  let pending: PendingAgentApproval | null = null;
  for (const event of events) {
    if (event.event !== "approval") continue;
    const data = objectData(event.data);
    const seq = data?.seq;
    if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq < 1) continue;
    if (typeof data?.decision === "string") {
      if (pending?.seq === seq) pending = null;
      continue;
    }
    const request = parseApproval(data?.request);
    if (request) pending = { seq, request };
  }
  return pending;
}

function parseModelCheck(value: unknown): ModelCheck | null {
  const data = objectData(value);
  if (!data || typeof data.recommended !== "boolean" || !Array.isArray(data.failed_checks)) return null;
  const failedChecks = data.failed_checks.filter((item): item is string => typeof item === "string");
  // Never show a model as recommended when the backend also listed a failure.
  return { recommended: data.recommended && failedChecks.length === 0, failedChecks };
}

/** Whether the probe is running now, and the latest result the run reported. */
function modelCheckState(events: readonly AgentSSEEvent[]): { checking: boolean; check: ModelCheck | null } {
  let checking = false;
  let check: ModelCheck | null = null;
  for (const event of events) {
    if (event.event !== "status") continue;
    const data = objectData(event.data);
    checking = data?.status === "pending" && data?.phase === "model_check";
    check = parseModelCheck(data?.model_check) ?? check;
  }
  return { checking, check };
}

function retryDelay(signal: AbortSignal, ms: number): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve();
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Fetch-stream SSE follower: EventSource cannot send the bearer token or the
 * Last-Event-ID header. A cursor survives each reconnect, and replayed packets
 * are ignored. Approval and terminal statuses pause or finish the follower.
 */
export function useAgentRun(api: AgentApi, enabled: boolean) {
  const [run, setRun] = useState<AgentRunResponse | null>(null);
  const [events, setEvents] = useState<AgentSSEEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"idle" | "live" | "reconnecting">("idle");
  const [busy, setBusy] = useState<"starting" | "deciding" | "stopping" | "resuming" | "opening" | null>(null);
  // Set when a recorded decision's Resume request failed; the server's
  // resume_required covers a reopened run (#682).
  const [resumeFailed, setResumeFailed] = useState(false);
  const [followVersion, setFollowVersion] = useState(0);
  const cursorRef = useRef(0);
  const statusRef = useRef<string | null>(null);
  // The run shown now. A late callback for another run must not touch it (#683).
  const currentRunRef = useRef<string | null>(null);
  const runId = run?.id ?? null;
  currentRunRef.current = runId;
  const resumeRequired = Boolean(run && !TERMINAL.has(run.status)
    && (resumeFailed || run.resume_required === true));

  useEffect(() => {
    if (!enabled || !runId) return;
    const controller = new AbortController();
    const signal = controller.signal;
    const onEvent = (event: AgentSSEEvent) => {
      if (signal.aborted || !event || event.run_id !== runId
        || !Number.isSafeInteger(event.event_id) || event.event_id <= cursorRef.current
        || !objectData(event.data)) return;
      cursorRef.current = event.event_id;
      setEvents((previous) => [...previous, event]);
      setConnection("live");
      setError(null);
      if (event.event === "status" && typeof event.data.status === "string"
        && KNOWN_STATUSES.has(event.data.status)) {
        statusRef.current = event.data.status;
        setRun((previous) => previous?.id === runId
          ? { ...previous, status: event.data.status as string,
            stop_reason: typeof event.data.stop_reason === "string" ? event.data.stop_reason : null }
          : previous);
      }
    };

    const follow = async () => {
      let delay = 350;
      // Even a fast-completed or already approval-parked run has replayable
      // history. Fetch it once before deciding that following is finished.
      let first = true;
      while (!signal.aborted && (first || (!TERMINAL.has(statusRef.current ?? "")
        && statusRef.current !== "waiting_approval"))) {
        first = false;
        try {
          await api.events(runId, onEvent, {
            signal,
            ...(cursorRef.current > 0 ? { afterEventId: cursorRef.current } : {}),
          });
        } catch (cause) {
          if (signal.aborted) return;
          // Auth, consent and missing-run errors are not transient connections.
          if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
            setError(describeApiError(cause, "This agent run is unavailable."));
            setConnection("idle");
            return;
          }
        }
        if (signal.aborted || TERMINAL.has(statusRef.current ?? "")
          || statusRef.current === "waiting_approval") break;
        try {
          // A clean EOF could mean an idle proxy, a terminal run, or approval.
          // Refresh server status instead of assuming that EOF means success.
          const fresh = await api.get(runId);
          if (signal.aborted) return;
          statusRef.current = fresh.status;
          setRun(fresh);
          if (TERMINAL.has(fresh.status) || fresh.status === "waiting_approval") break;
        } catch (cause) {
          if (signal.aborted) return;
          if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
            setError(describeApiError(cause, "This agent run is unavailable."));
            setConnection("idle");
            return;
          }
        }
        setConnection("reconnecting");
        await retryDelay(signal, delay);
        delay = Math.min(delay * 2, 4000);
      }
      if (!signal.aborted) setConnection("idle");
    };
    void follow();
    return () => controller.abort();
  }, [api, enabled, runId, followVersion]);

  const pendingApproval = useMemo(
    () => run?.status === "waiting_approval" ? latestApproval(events) : null,
    [events, run?.status],
  );
  const { checking: checkingModel, check: modelCheck } = useMemo(() => modelCheckState(events), [events]);

  const start = async (payload: StartAgentRun): Promise<boolean> => {
    if (!enabled || busy || payload.confirmed_project !== true
      || (cloudModelProvider(payload.model) && payload.confirmed_cloud !== true)) return false;
    setBusy("starting");
    setError(null);
    try {
      const created = await api.start(payload);
      cursorRef.current = 0;
      statusRef.current = created.status;
      currentRunRef.current = created.id;
      setEvents([]);
      setResumeFailed(false);
      setRun(created);
      setFollowVersion((previous) => previous + 1);
      return true;
    } catch (cause) {
      setError(describeApiError(cause, "Could not start the agent. Check the project folder and model."));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const open = async (id: string): Promise<void> => {
    if (!enabled || busy || !id) return;
    setBusy("opening");
    setError(null);
    try {
      const selected = await api.get(id);
      if (selected.id !== id) throw new Error("The requested run was not returned.");
      cursorRef.current = 0;
      statusRef.current = selected.status;
      currentRunRef.current = selected.id;
      setEvents([]);
      setResumeFailed(false);
      setRun(selected);
      setFollowVersion((previous) => previous + 1);
    } catch (cause) {
      setError(describeApiError(cause, "Could not open this agent run."));
    } finally {
      setBusy(null);
    }
  };

  // Compare with the run shown now, not the one of the render that created
  // this callback: an acknowledgement can arrive after another run opened.
  const forget = (id: string): void => {
    if (currentRunRef.current !== id) return;
    cursorRef.current = 0;
    statusRef.current = null;
    currentRunRef.current = null;
    setEvents([]);
    setResumeFailed(false);
    setRun(null);
  };

  /** After a decision error, show what the server now holds and follow it again. */
  const reconcile = async (id: string): Promise<void> => {
    try {
      const fresh = await api.get(id);
      if (currentRunRef.current !== id || fresh.id !== id) return;
      statusRef.current = fresh.status;
      setRun(fresh);
      setFollowVersion((previous) => previous + 1);
    } catch {
      // Keep the error already shown.
    }
  };

  /** Re-read the shown run's state, e.g. after its work copy was discarded (#680). */
  const refresh = async (id: string): Promise<void> => {
    if (!enabled || currentRunRef.current !== id) return;
    try {
      const fresh = await api.get(id);
      if (currentRunRef.current !== id || fresh.id !== id) return;
      statusRef.current = fresh.status;
      setRun(fresh);
    } catch {
      // The panel shows the run as it was; the next open reads it again.
    }
  };

  const decide = async (seq: number, decision: DecideAgentApproval["decision"]): Promise<void> => {
    if (!enabled || busy || !run || !pendingApproval || pendingApproval.seq !== seq) return;
    setBusy("deciding");
    setError(null);
    try {
      // The server commits a one-use decision. Only then may the parked worker
      // be explicitly resumed; never infer consent from a model event.
      const decided = await api.decide(run.id, seq, decision);
      statusRef.current = decided.status;
      setRun(decided);
      try {
        const resumed = await api.resume(run.id);
        statusRef.current = resumed.status;
        setRun(resumed);
        setResumeFailed(false);
        setFollowVersion((previous) => previous + 1);
      } catch (cause) {
        setResumeFailed(true);
        setError(describeApiError(cause, "Approval was saved, but the run could not resume. Choose Resume run."));
      }
    } catch (cause) {
      setError(describeApiError(cause, "This approval could not be saved. Review it again."));
      // The approval may have expired, or the decision may have been saved
      // before the response was lost: show the run as the server has it.
      // Nothing is approved or resumed automatically (#677, #682).
      await reconcile(run.id);
    } finally {
      setBusy(null);
    }
  };

  const resume = async (): Promise<void> => {
    if (!enabled || !run || busy || !resumeRequired) return;
    setBusy("resuming");
    setError(null);
    try {
      const updated = await api.resume(run.id);
      statusRef.current = updated.status;
      setRun(updated);
      setResumeFailed(false);
      setFollowVersion((previous) => previous + 1);
    } catch (cause) {
      setError(describeApiError(cause, "Could not resume the run."));
    } finally {
      setBusy(null);
    }
  };

  const stop = async (): Promise<void> => {
    if (!enabled || !run || busy || TERMINAL.has(run.status)) return;
    setBusy("stopping");
    setError(null);
    try {
      const cancelled = await api.stop(run.id);
      statusRef.current = cancelled.status;
      setRun(cancelled);
      setResumeFailed(false);
      // A stream parked at an approval must wake up to show cancellation.
      setFollowVersion((previous) => previous + 1);
    } catch (cause) {
      setError(describeApiError(cause, "Could not stop the run."));
    } finally {
      setBusy(null);
    }
  };

  return {
    run, events, pendingApproval, modelCheck, checkingModel, error, connection, busy, resumeRequired,
    start, open, forget, refresh, decide, resume, stop,
  };
}
