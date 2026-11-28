// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { ExecutionTaskSummary } from "../../../contracts/cortex-api";
import { mergeExecutionTasks } from "./executionTasks";

const task = (job_id: string, sequence: number, extra: Partial<ExecutionTaskSummary> = {}): ExecutionTaskSummary => ({
  job_id, profile: "code", status: "queued", sequence,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", ...extra,
});

describe("mergeExecutionTasks", () => {
  it("never lets an older listing undo a confirmed approval (#722)", () => {
    const shown = [
      task("a", 4, { status: "running", approval_state: "approved" }),
      task("b", 2),
    ];
    // Snapshotted before the approval, but newer for b.
    const listed = [task("a", 3, { approval_state: "pending" }), task("b", 5, { status: "running" })];

    expect(mergeExecutionTasks(listed, shown)).toEqual([shown[0], listed[1]]);
  });

  it("keeps a decision the listing at the same sequence has not seen", () => {
    const shown = [task("a", 3, { approval_state: "denied" })];
    expect(mergeExecutionTasks([task("a", 3, { approval_state: "pending" })], shown)).toEqual(shown);
  });

  it("takes the listing for newer states, new tasks, and drops tasks no longer listed", () => {
    const shown = [task("a", 1, { approval_state: "pending" }), task("gone", 1)];
    const listed = [task("a", 2, { approval_state: "approved" }), task("new", 1)];
    expect(mergeExecutionTasks(listed, shown)).toEqual(listed);
  });
});
