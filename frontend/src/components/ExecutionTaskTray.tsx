import { useState } from "react";
import type { ExecutionTaskSummary } from "../../../contracts/cortex-api";

type Props = {
  tasks: ExecutionTaskSummary[];
  onCancel?: (jobId: string) => Promise<void>;
};

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

export function ExecutionTaskTray({ tasks, onCancel }: Props) {
  const [cancelling, setCancelling] = useState<Set<string>>(() => new Set());
  const activeTasks = tasks.filter((task) => ACTIVE_STATUSES.has(task.status));
  if (!tasks.length) return null;

  const announce = activeTasks.length
    ? `${activeTasks.length} background task${activeTasks.length === 1 ? "" : "s"} in progress.`
    : "Background tasks complete.";

  const stop = async (jobId: string) => {
    if (!onCancel || cancelling.has(jobId)) return;
    setCancelling((current) => new Set(current).add(jobId));
    try {
      await onCancel(jobId);
    } finally {
      setCancelling((current) => {
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
        <span className="execution-task-tray-count" aria-hidden="true">{tasks.length}</span>
      </div>
      <div className="execution-task-tray-live" aria-live="polite" role="status">{announce}</div>
      <ul className="execution-task-list">
        {tasks.map((task) => {
          const canStop = Boolean(task.can_cancel) && ACTIVE_STATUSES.has(task.status) && Boolean(onCancel);
          const isCancelling = cancelling.has(task.job_id) || task.status === "cancelling";
          return (
            <li className="execution-task" key={task.job_id}>
              <div className="execution-task-copy">
                <div className="execution-task-label">
                  {ACTIVE_STATUSES.has(task.status) && <span className="loading-spinner execution-task-spinner" aria-hidden="true" />}
                  <strong>{task.message || task.phase || "Working"}</strong>
                </div>
                <span>{formatTaskStatus(task.status)}</span>
              </div>
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

function formatTaskStatus(status: ExecutionTaskSummary["status"]): string {
  return status === "cancelling" ? "Stopping" : `${status[0].toUpperCase()}${status.slice(1)}`;
}
