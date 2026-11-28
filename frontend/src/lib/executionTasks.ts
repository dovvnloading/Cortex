import type { ExecutionTaskSummary } from "../../../contracts/cortex-api";

const DECIDED = new Set(["approved", "denied", "expired"]);

/**
 * Apply a task list to the tasks on screen, keeping any task the screen
 * already knows a newer state of.
 *
 * A list request can be answered after a decision on one of its tasks was
 * confirmed: replacing the whole list then showed an approved task as pending
 * again, with its Allow/Deny buttons (#722). A listed task replaces the shown
 * one unless the shown one is further along -- a higher sequence, or a decision
 * the listing (at the same sequence) has not seen yet.
 */
export function mergeExecutionTasks(
  listed: readonly ExecutionTaskSummary[],
  shown: readonly ExecutionTaskSummary[],
): ExecutionTaskSummary[] {
  const byId = new Map(shown.map((task) => [task.job_id, task]));
  return listed.map((incoming) => {
    const local = byId.get(incoming.job_id);
    if (!local) return incoming;
    if (local.sequence > incoming.sequence) return local;
    if (
      local.sequence === incoming.sequence
      && incoming.approval_state === "pending"
      && local.approval_state !== undefined
      && DECIDED.has(local.approval_state)
    ) return local;
    return incoming;
  });
}
