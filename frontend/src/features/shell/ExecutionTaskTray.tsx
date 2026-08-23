import { AlertTriangle, Check, ChevronDown, Code2, X } from "lucide-react";
import { useState } from "react";
import type { CodeExecutionSourceResponse, ExecutionApprovalDecisionRequest, ExecutionTaskSummary } from "../../../../contracts/cortex-api";

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
  const [sourceErrors, setSourceErrors] = useState<Map<string, string>>(() => new Map());
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
  const latestTerminalTask = tasks
    .filter((task) => !ACTIVE_STATUSES.has(task.status) && task.approval_state !== "pending")
    .reduce<ExecutionTaskSummary | null>((latest, task) => (
      !latest || task.updated_at > latest.updated_at ? task : latest
    ), null);
  const completionDismissed = !hasLiveWork && Boolean(completionKey) && dismissedCompletionKey === completionKey;
  if (!tasks.length || completionDismissed) return null;

  const announce = pendingApprovals.length
    ? `${pendingApprovals.length} task${pendingApprovals.length === 1 ? " requires" : "s require"} your approval.`
    : activeTasks.length
      ? `${activeTasks.length} local task${activeTasks.length === 1 ? " is" : "s are"} running.`
      : latestTerminalTask?.status === "failed"
        ? "The latest local task failed."
        : latestTerminalTask?.status === "cancelled"
          ? "The latest local task was cancelled."
      : "The latest local task is complete.";

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

  const loadSource = async (task: ExecutionTaskSummary) => {
    if (!onLoadCodeSource || loadingSource.has(task.job_id)) return;
    const existing = codeSources.get(task.job_id);
    if (existing && sourceMetadataMatchesTask(task, existing)) return;
    setLoadingSource((current) => new Set(current).add(task.job_id));
    setSourceErrors((current) => {
      const next = new Map(current);
      next.delete(task.job_id);
      return next;
    });
    try {
      const source = await onLoadCodeSource(task.job_id);
      if (!(await sourceMatchesTask(task, source))) {
        throw new Error("source_verification_failed");
      }
      setCodeSources((current) => new Map(current).set(task.job_id, source));
    } catch {
      setCodeSources((current) => {
        const next = new Map(current);
        next.delete(task.job_id);
        return next;
      });
      setSourceErrors((current) => new Map(current).set(
        task.job_id,
        "The generated source could not be verified. Retry the review or deny this task.",
      ));
    } finally {
      setLoadingSource((current) => {
        const next = new Set(current);
        next.delete(task.job_id);
        return next;
      });
    }
  };

  return (
    <aside className={`execution-task-tray${hasLiveWork ? " execution-task-tray-active" : ""}`} aria-label="Task activity">
      <div className="execution-task-tray-heading">
        <div className="execution-task-tray-title">
          <span className="execution-task-tray-kicker">TASK ACTIVITY</span>
          <h2>{pendingApprovals.length ? "Review before running" : hasLiveWork ? "Running locally" : "Recent activity"}</h2>
        </div>
        <div className="execution-task-tray-controls">
          <span className="execution-task-tray-count" aria-label={`${tasks.length} task${tasks.length === 1 ? "" : "s"}`}>{tasks.length}</span>
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
          const isCodeTask = task.profile === "code.exec.v1";

          return (
            <li
              className={`execution-task ${approvalPending ? "execution-task-approval" : ""} ${isCodeTask ? "execution-task-code" : ""}`}
              key={group.key}
              aria-busy={approvalDecision ? "true" : undefined}
            >
              {isCodeTask ? (
                <CodeTaskSummary
                  task={task}
                  approvalPending={approvalPending}
                  approvalDecision={approvalDecision}
                  showsWorking={showsWorking}
                  groupCount={group.count}
                  codeSource={codeSources.get(task.job_id)}
                  loadingSource={loadingSource.has(task.job_id)}
                  sourceError={sourceErrors.get(task.job_id)}
                  onLoadSource={onLoadCodeSource ? () => void loadSource(task) : undefined}
                  onDecideApproval={onDecideApproval ? (decision) => void decide(task.job_id, decision) : undefined}
                />
              ) : (
                <div className="execution-task-copy">
                  <div className="execution-task-label">
                    {showsWorking && <span className="loading-spinner execution-task-spinner" aria-hidden="true" />}
                    <strong>{approvalPending ? task.approval_reason || "Approval required" : displayMessage}</strong>
                  </div>
                  <span>{approvalPending ? formatApprovalMeta(task) : formatTaskStatus(task.status)}</span>
                </div>
              )}
              {!isCodeTask && approvalPending && onDecideApproval && (
                <ApprovalActions task={task} decision={approvalDecision} onDecide={(value) => void decide(task.job_id, value)} />
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

function CodeTaskSummary({
  task,
  approvalPending,
  approvalDecision,
  showsWorking,
  groupCount,
  codeSource,
  loadingSource,
  sourceError,
  onLoadSource,
  onDecideApproval,
}: {
  task: ExecutionTaskSummary;
  approvalPending: boolean;
  approvalDecision?: ExecutionApprovalDecision;
  showsWorking: boolean;
  groupCount: number;
  codeSource?: CodeExecutionSourceResponse;
  loadingSource: boolean;
  sourceError?: string;
  onLoadSource?: () => void;
  onDecideApproval?: (decision: ExecutionApprovalDecision) => void;
}) {
  const title = task.intent_summary || task.approval_reason || task.message || "Local Python task";
  const status = approvalPending ? "Approval needed" : formatTaskStatus(task.status);
  const sourceReviewed = Boolean(codeSource && sourceMetadataMatchesTask(task, codeSource));
  return (
    <div className="execution-task-code-content">
      <div className="execution-task-code-heading">
        <div className="execution-task-code-title">
          <span className="execution-task-code-icon" aria-hidden="true"><Code2 size={15} /></span>
          <div>
            <strong>{groupCount > 1 ? `${groupCount} × ${title}` : title}</strong>
            <span>{status}</span>
          </div>
        </div>
        <CodeTaskState task={task} approvalPending={approvalPending} showsWorking={showsWorking} />
      </div>
      {task.capabilities && (
        <div className="execution-task-capability-row" aria-label="Requested host access">
          {formatCapabilities(task.capabilities).map((capability) => <span key={capability} className="execution-task-capability">{capability}</span>)}
        </div>
      )}
      {task.capabilities && hasBroadCapabilities(task.capabilities) && (
        <div className="execution-task-warning" role="note"><AlertTriangle size={13} aria-hidden="true" /><span>Broad local access requested. Review the source before allowing.</span></div>
      )}
      {task.status === "failed" && (
        <div className="execution-task-failure" role="alert"><AlertTriangle size={13} aria-hidden="true" /><span>{formatCodeFailure(task)}</span></div>
      )}
      {approvalPending && !sourceReviewed && (
        <div className="execution-task-warning" role="note"><AlertTriangle size={13} aria-hidden="true" /><span>Open and verify the generated source before allowing this task.</span></div>
      )}
      {task.result && <CodeResult result={task.result} />}
      {onLoadSource && (
        <details className="execution-task-code-details" onToggle={(event) => { if (event.currentTarget.open) onLoadSource(); }}>
          <summary><ChevronDown size={13} aria-hidden="true" />Review generated source</summary>
          {loadingSource && <span className="execution-task-source-loading">Loading source…</span>}
          {sourceError && (
            <div className="execution-task-failure" role="alert">
              <AlertTriangle size={13} aria-hidden="true" />
              <span>{sourceError}</span>
              <button className="button button-secondary execution-task-decision" type="button" onClick={onLoadSource} disabled={loadingSource}>Retry</button>
            </div>
          )}
          {codeSource && <div className="execution-task-source">
            <span>Digest {codeSource.source_digest.slice(0, 16)}…</span>
            <pre>{codeSource.source}</pre>
          </div>}
        </details>
      )}
      {approvalPending && onDecideApproval && (
        <ApprovalActions
          task={task}
          decision={approvalDecision}
          onDecide={onDecideApproval}
          allowDisabled={!sourceReviewed}
        />
      )}
    </div>
  );
}

function ApprovalActions({ task, decision, onDecide, allowDisabled = false }: { task: ExecutionTaskSummary; decision?: ExecutionApprovalDecision; onDecide: (decision: ExecutionApprovalDecision) => void; allowDisabled?: boolean }) {
  return (
    <div className="execution-task-approval-actions" aria-label={`Approval actions for background task ${task.job_id}`}>
      <button className="button button-primary execution-task-decision" type="button" onClick={() => onDecide("approved")} disabled={Boolean(decision) || allowDisabled} aria-label={`Allow background task ${task.job_id} once`} title={allowDisabled ? "Review and verify the generated source first." : undefined}>
        {decision === "approved" ? "Allowing…" : "Allow once"}
      </button>
      <button className="button button-secondary execution-task-decision" type="button" onClick={() => onDecide("denied")} disabled={Boolean(decision)} aria-label={`Deny background task ${task.job_id}`}>
        {decision === "denied" ? "Denying…" : "Deny"}
      </button>
    </div>
  );
}

function sourceMetadataMatchesTask(
  task: ExecutionTaskSummary,
  source: CodeExecutionSourceResponse,
): boolean {
  if (
    task.profile !== "code.exec.v1"
    || source.job_id !== task.job_id
    || source.language !== "python"
    || !task.source_digest
    || source.source_digest !== task.source_digest
    || !task.intent_summary
    || source.intent_summary !== task.intent_summary
    || !task.capabilities
  ) {
    return false;
  }
  return (["filesystem", "process", "network"] as const).every((capability) => (
    typeof source.capabilities[capability] === "boolean"
    && source.capabilities[capability] === task.capabilities?.[capability]
  ));
}

async function sourceMatchesTask(
  task: ExecutionTaskSummary,
  source: CodeExecutionSourceResponse,
): Promise<boolean> {
  if (!sourceMetadataMatchesTask(task, source) || !globalThis.crypto?.subtle) return false;
  const encoded = new TextEncoder().encode(source.source);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", encoded);
  const actualDigest = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  return actualDigest === source.source_digest;
}

function CodeTaskState({
  task,
  approvalPending,
  showsWorking,
}: {
  task: ExecutionTaskSummary;
  approvalPending: boolean;
  showsWorking: boolean;
}) {
  if (approvalPending) {
    return <span className="execution-task-state execution-task-state-review">Review</span>;
  }
  if (showsWorking) {
    return <span className="execution-task-state execution-task-state-running">Running</span>;
  }
  if (task.status === "failed") {
    return <span className="execution-task-state execution-task-state-failed"><AlertTriangle size={12} aria-hidden="true" />Failed</span>;
  }
  if (task.status === "cancelled") {
    return <span className="execution-task-state execution-task-state-cancelled">Cancelled</span>;
  }
  return <span className="execution-task-state execution-task-state-complete"><Check size={12} aria-hidden="true" />Complete</span>;
}

function CodeResult({ result }: { result: Record<string, unknown> }) {
  const stdout = typeof result.stdout === "string" ? result.stdout : "";
  const stderr = typeof result.stderr === "string" ? result.stderr : "";
  const value = result.value === undefined || result.value === null ? "" : JSON.stringify(result.value);
  const duration = typeof result.duration_ms === "number" && Number.isFinite(result.duration_ms)
    ? `${Math.max(0, Math.round(result.duration_ms))} ms`
    : null;
  return (
    <div className="execution-task-result">
      <div className="execution-task-result-heading"><span>Result</span><span>{duration && <span className="execution-task-result-duration">{duration}</span>} {result.truncated === true && <span className="execution-task-result-truncated">Output truncated</span>}</span></div>
      {stdout && <div><span className="execution-task-result-label">Output</span><pre>{stdout}</pre></div>}
      {stderr && <div><span className="execution-task-result-label execution-task-result-error">Errors</span><pre className="execution-task-result-stderr">{stderr}</pre></div>}
      {value && <div><span className="execution-task-result-label">Value</span><pre>{value}</pre></div>}
      {!stdout && !stderr && !value && <span className="execution-task-result-empty">No output returned.</span>}
    </div>
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
    const key = task.profile === "code.exec.v1" || !terminal
      ? `job:${task.job_id}`
      : JSON.stringify(["terminal", task.profile, task.status, task.phase ?? "", task.message ?? ""]);
    const existing = groups.get(key);
    if (existing) existing.count += 1;
    else groups.set(key, { key, task, count: 1 });
  }
  return [...groups.values()];
}

function formatTaskStatus(status: ExecutionTaskSummary["status"]): string {
  return status === "cancelling" ? "Stopping" : `${status[0].toUpperCase()}${status.slice(1)}`;
}

function formatCodeFailure(task: ExecutionTaskSummary): string {
  const messages: Record<string, string> = {
    worker_startup_timeout: "The isolated worker did not finish starting in time.",
    worker_timeout: "The isolated worker timed out before returning output.",
    worker_failed: "The isolated worker stopped before returning output.",
    worker_output_invalid: "The isolated worker returned an invalid result.",
    process_isolation_unavailable: "The required process isolation could not be established.",
    process_capability_unavailable: "Process access is unavailable until native sandbox isolation is enabled.",
    runtime_limit: "The code exceeded the execution limits.",
    memory_limit: "The code exceeded the memory limit.",
  };
  if (task.error && messages[task.error]) return messages[task.error];
  if (task.message && task.message !== "Local code execution failed safely.") return task.message;
  return "Local code execution failed safely.";
}

function formatCapabilities(capabilities: NonNullable<ExecutionTaskSummary["capabilities"]>): string[] {
  const labels: Record<string, string> = { filesystem: "Files", process: "Processes", network: "Network" };
  const requested = Object.entries(capabilities).filter(([, enabled]) => enabled).map(([name]) => labels[name] ?? name);
  return requested.length ? requested : ["No host access requested"];
}

function hasBroadCapabilities(capabilities: NonNullable<ExecutionTaskSummary["capabilities"]>): boolean {
  return Object.values(capabilities).some(Boolean);
}
