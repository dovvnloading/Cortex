import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExecutionTaskSummary } from "../../../contracts/cortex-api";
import { ExecutionTaskTray } from "./ExecutionTaskTray";

const task: ExecutionTaskSummary = {
  job_id: "job-1",
  profile: "fake.v1",
  status: "running",
  sequence: 4,
  phase: "compute",
  message: "Fake step 2 of 3.",
  can_cancel: true,
  created_at: "2026-07-21T00:00:00Z",
  updated_at: "2026-07-21T00:00:01Z",
};

describe("ExecutionTaskTray", () => {
  it("announces active work and exposes an accessible Stop action", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn<(jobId: string) => Promise<void>>().mockResolvedValue();
    render(<ExecutionTaskTray tasks={[task]} onCancel={onCancel} />);

    expect(screen.getByRole("complementary", { name: "Background tasks" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1 background task in progress.");
    expect(screen.getByRole("complementary").querySelector(".execution-task-spinner")).toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "Stop background task job-1" });
    expect(stop).toBeEnabled();
    await user.click(stop);
    expect(onCancel).toHaveBeenCalledWith("job-1");
  });

  it("keeps terminal state visible without offering Stop", () => {
    render(<ExecutionTaskTray tasks={[{ ...task, status: "succeeded", can_cancel: false, message: "Complete" }]} />);
    expect(screen.getByRole("status")).toHaveTextContent("Background tasks complete.");
    expect(screen.queryByRole("button", { name: /Stop background task/ })).not.toBeInTheDocument();
  });
});
