import { useState } from "react";
import { X } from "lucide-react";
import type { CodeExecutionSourceResponse, ExecutionApprovalDecisionRequest, ExecutionTaskSummary } from "../../../contracts/cortex-api";

type ExecutionApprovalDecision = ExecutionApprovalDecisionRequest["decision"];

type Props = {
  tasks: ExecutionTaskSummary[];
  onCancel?: (jobId: string) => Promise<void>;
  onDecideApproval?: (jobId: string, decision: ExecutionApprovalDecision) => Promise<void>;
  onLoadCodeSource?: (jobId: string) => Promise<CodeExecutionSourceResponse>;
};

type TaskGroup = {
  key: string;
  task: ExecutionTaskSummary;
  count: number;
};

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

export function ExecutionTaskTray({ tasks, onCancel, onDecideApproval, onLoadCodeSource }: Props) {
  const [cancelling, setCancelling] = useState<Set<string>>(() => new Set());
  const [deciding, setDeciding] = useState<Map<string, ExecutionApprovalDecision>>(() => new Map());
  const [dismissedCompletionKey, setDismissedCompletionKey] = useState<string | null>(null);
  const [codeSources, setCodeSources] = useState<Map<string, CodeExecutionSourceResponse>>(() => new Map());
  const [loadingSource, setLoadingSource] = useState<Set<string>>(() => new Set());
  const taskGroups = groupTasks(tasks);
  const activeTasks = tasks.filter((task) => ACTIVE_STATUSES.has(task.status)
    && !["pending", "denied", "expired"].includes(task.approval_state ?? "not_required"));
  const pendingApprovals = tasks.filter((task) => task.approval_state === "pending");
  const hasLiveWork = activeTasks.length > 0 || pendingApprovals.length > 0;
  const completionKey = tasks
    .filter((task) => !ACTIVE_STATUSES.has(task.status) && task.approval_state !== "pending")
    .map((task) => `${task.job_id}:${task.sequence}:${task.status}:${task.updated_at}`)
    .sort()
    .join("|");
  const completionDismissed = !hasLiveWork && Boolean(completionKey) && dismissedCompletionKey === completionKey;
  if (!tasks.length) return null;
  if (completionDismissed) return null;

  const announce = pendingApprovals.length
    ? `${pendingApprovals.length} background task${pendingApprovals.length === 1 ? " requires" : "s require"} approval.`
    : activeTasks.length
    ? `${activeTasks.length} background task${activeTasks.length === 1 ? "" : "s"} in progress.`
    : "Background tasks complete.";

  const stop = async (jobId: string) => {
    if (!onCancel || cancelling.has(jobId)) return;
    setCancelling((current) => new Set(current).add(jobId));
    try {
      await onCancel(jobId);
    } catch {
      // The workspace callback owns user-visible error reporting.
    } finally {
      setCancelling((current) => {
        const next = new Set(current);
        next.delete(jobId);
        return next;
      });
    }
  };

  const decide = async (jobId: string, decision: ExecutionApprovalDecision) => {
    if (!onDecideApproval || deciding.has(jobId)) return;
    setDeciding((current) => new Map(current).set(jobId, decision));
    try {
      await onDecideApproval(jobId, decision);
    } catch {
      // The workspace callback owns user-visible error reporting.
    } finally {
      setDeciding((current) => {
        const next = new Map(current);
        next.delete(jobId);
        return next;
      });
    }
  };

  const loadSource = async (jobId: string) => {
    if (!onLoadCodeSource || codeSources.has(jobId) || loadingSource.has(jobId)) return;
    setLoadingSource((current) => new Set(current).add(jobId));
    try {
      const source = await onLoadCodeSource(jobId);
      setCodeSources((current) => new Map(current).set(jobId, source));
    } catch {
      // The source remains collapsed if it is no longer available.
    } finally {
      setLoadingSource((current) => {
        const next = new Set(current);
        next.delete(jobId);
        return next;
      });
    }
  };

  return (
    <aside className="execution-task-tray" aria-label="Background tasks">
      <div className="execution-task-tray-heading">
        <h2>Background tasks</h2>
        <div className="execution-task-tray-controls">
          <span className="execution-task-tray-count" aria-hidden="true">{tasks.length}</span>
          {!hasLiveWork && Boolean(completionKey) && (
            <button
              className="execution-task-tray-dismiss"
              type="button"
              onClick={() => setDismissedCompletionKey(completionKey)}
              aria-label="Dismiss completed background task notification"
              title="Dismiss notification"
            >
              <X aria-hidden="true" size={15} strokeWidth={2.25} />
            </button>
          )}
        </div>
      </div>
      <div className="execution-task-tray-live" aria-live="polite" role="status">{announce}</div>
      <ul className="execution-task-list">
        {taskGroups.map((group) => {
          const task = group.task;
          const approvalPending = task.approval_state === "pending";
          const approvalDecision = deciding.get(task.job_id);
          const showsWorking = ACTIVE_STATUSES.has(task.status)
            && !["pending", "denied", "expired"].includes(task.approval_state ?? "not_required");
          const canStop = !approvalPending && Boolean(task.can_cancel) && ACTIVE_STATUSES.has(task.status) && Boolean(onCancel);
          const isCancelling = cancelling.has(task.job_id) || task.status === "cancelling";
          const taskMessage = task.message || task.phase || "Working";
          const displayMessage = group.count > 1 ? `${group.count} × ${taskMessage}` : taskMessage;
          return (
            <li
              className={`execution-task ${approvalPending ? "execution-task-approval" : ""}`}
              key={group.key}
              aria-busy={approvalDecision ? "true" : undefined}
            >
              <div className="execution-task-copy">
                <div className="execution-task-label">
                  {showsWorking && <span className="loading-spinner execution-task-spinner" aria-hidden="true" />}
                  <strong>{approvalPending ? task.approval_reason || "Approval required" : displayMessage}</strong>
                </div>
                <span>{approvalPending ? formatApprovalMeta(task) : formatTaskStatus(task.status)}</span>
                {task.profile === "code.exec.v1" && (
                  <>
                    {task.capabilities && <span className="execution-task-capabilities">{formatCapabilities(task.capabilities)}</span>}
                    {task.capabilities && hasBroadCapabilities(task.capabilities) && <span className="execution-task-warning">High-risk local access · review before allowing</span>}
                    {task.result && <pre className="execution-task-output">{formatCodeResult(task.result)}</pre>}
                    {onLoadCodeSource && <details className="execution-task-code-details" onToggle={(event) => { if (event.currentTarget.open) void loadSource(task.job_id); }}>
                      <summary>Review generated code</summary>
                      {loadingSource.has(task.job_id) && <span>Loading source…</span>}
                      {codeSources.get(task.job_id) && <><span>Digest {codeSources.get(task.job_id)?.source_digest.slice(0, 16)}…</span><pre>{codeSources.get(task.job_id)?.source}</pre></>}
                    </details>}
                  </>
                )}
              </div>
              {approvalPending && onDecideApproval && (
                <div className="execution-task-approval-actions" aria-label={`Approval actions for background task ${task.job_id}`}>
                  <button
                    className="button button-primary execution-task-decision"
                    type="button"
                    onClick={() => void decide(task.job_id, "approved")}
                    disabled={Boolean(approvalDecision)}
                    aria-label={`Allow background task ${task.job_id} once`}
                  >
                    {approvalDecision === "approved" ? "Allowing…" : "Allow once"}
                  </button>
                  <button
                    className="button button-secondary execution-task-decision"
                    type="button"
                    onClick={() => void decide(task.job_id, "denied")}
                    disabled={Boolean(approvalDecision)}
                    aria-label={`Deny background task ${task.job_id}`}
                  >
                    {approvalDecision === "denied" ? "Denying…" : "Deny"}
                  </button>
                </div>
              )}
              {canStop && (
                <button
                  className="button button-secondary execution-task-stop"
                  type="button"
                  onClick={() => void stop(task.job_id)}
                  disabled={isCancelling}
                  aria-label={`Stop background task ${task.job_id}`}
                >
                  {isCancelling ? "Stopping…" : "Stop"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

function formatApprovalMeta(task: ExecutionTaskSummary): string {
  const profile = task.profile.replace(/\.v\d+$/, "").replaceAll(".", " ");
  if (!task.approval_expires_at) return `Action required · ${profile}`;
  const expires = new Date(task.approval_expires_at);
  const expiry = Number.isNaN(expires.getTime())
    ? "expiry unavailable"
    : `expires ${expires.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  return `Action required · ${profile} · ${expiry}`;
}

function groupTasks(tasks: ExecutionTaskSummary[]): TaskGroup[] {
  const groups = new Map<string, TaskGroup>();
  for (const task of tasks) {
    const approvalPending = task.approval_state === "pending";
    const terminal = !ACTIVE_STATUSES.has(task.status) && !approvalPending;
    const key = terminal
      ? JSON.stringify(["terminal", task.profile, task.status, task.phase ?? "", task.message ?? ""])
      : `job:${task.job_id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      groups.set(key, { key, task, count: 1 });
    }
  }
  return [...groups.values()];
}

function formatTaskStatus(status: ExecutionTaskSummary["status"]): string {
  return status === "cancelling" ? "Stopping" : `${status[0].toUpperCase()}${status.slice(1)}`;
}

function formatCapabilities(capabilities: NonNullable<ExecutionTaskSummary["capabilities"]>): string {
  const labels = Object.entries(capabilities).filter(([, enabled]) => enabled).map(([name]) => name);
  return labels.length ? `Requested: ${labels.join(", ")}` : "No host access requested";
}

function hasBroadCapabilities(capabilities: NonNullable<ExecutionTaskSummary["capabilities"]>): boolean {
  return Object.values(capabilities).some(Boolean);
}

function formatCodeResult(result: Record<string, unknown>): string {
  const stdout = typeof result.stdout === "string" ? result.stdout : "";
  const stderr = typeof result.stderr === "string" ? result.stderr : "";
  const value = result.value === undefined || result.value === null ? "" : `Value: ${JSON.stringify(result.value)}`;
  return [stdout && `Output:\n${stdout}`, stderr && `Errors:\n${stderr}`, value].filter(Boolean).join("\n\n") || "No output.";
}
