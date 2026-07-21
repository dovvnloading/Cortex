import { useState } from "react";
import type { ExecutionApprovalDecisionRequest, ExecutionTaskSummary } from "../../../contracts/cortex-api";

type ExecutionApprovalDecision = ExecutionApprovalDecisionRequest["decision"];

type Props = {
  tasks: ExecutionTaskSummary[];
  onCancel?: (jobId: string) => Promise<void>;
  onDecideApproval?: (jobId: string, decision: ExecutionApprovalDecision) => Promise<void>;
};

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

export function ExecutionTaskTray({ tasks, onCancel, onDecideApproval }: Props) {
  const [cancelling, setCancelling] = useState<Set<string>>(() => new Set());
  const [deciding, setDeciding] = useState<Map<string, ExecutionApprovalDecision>>(() => new Map());
  const activeTasks = tasks.filter((task) => ACTIVE_STATUSES.has(task.status)
    && !["pending", "denied", "expired"].includes(task.approval_state ?? "not_required"));
  const pendingApprovals = tasks.filter((task) => task.approval_state === "pending");
  if (!tasks.length) return null;

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

  return (
    <aside className="execution-task-tray" aria-label="Background tasks">
      <div className="execution-task-tray-heading">
        <h2>Background tasks</h2>
        <span className="execution-task-tray-count" aria-hidden="true">{tasks.length}</span>
      </div>
      <div className="execution-task-tray-live" aria-live="polite" role="status">{announce}</div>
      <ul className="execution-task-list">
        {tasks.map((task) => {
          const approvalPending = task.approval_state === "pending";
          const approvalDecision = deciding.get(task.job_id);
          const showsWorking = ACTIVE_STATUSES.has(task.status)
            && !["pending", "denied", "expired"].includes(task.approval_state ?? "not_required");
          const canStop = !approvalPending && Boolean(task.can_cancel) && ACTIVE_STATUSES.has(task.status) && Boolean(onCancel);
          const isCancelling = cancelling.has(task.job_id) || task.status === "cancelling";
          return (
            <li
              className={`execution-task ${approvalPending ? "execution-task-approval" : ""}`}
              key={task.job_id}
              aria-busy={approvalDecision ? "true" : undefined}
            >
              <div className="execution-task-copy">
                <div className="execution-task-label">
                  {showsWorking && <span className="loading-spinner execution-task-spinner" aria-hidden="true" />}
                  <strong>{approvalPending ? task.approval_reason || "Approval required" : task.message || task.phase || "Working"}</strong>
                </div>
                <span>{approvalPending ? formatApprovalMeta(task) : formatTaskStatus(task.status)}</span>
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

function formatTaskStatus(status: ExecutionTaskSummary["status"]): string {
  return status === "cancelling" ? "Stopping" : `${status[0].toUpperCase()}${status.slice(1)}`;
}
