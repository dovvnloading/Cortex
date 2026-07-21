import { render, screen, waitFor } from "@testing-library/react";
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

  it("renders pending approval as a non-modal action card without a spinner", async () => {
    const user = userEvent.setup();
    let finishDecision: (() => void) | undefined;
    const onDecideApproval = vi.fn<(jobId: string, decision: "approved" | "denied") => Promise<void>>(
      () => new Promise<void>((resolve) => { finishDecision = resolve; }),
    );
    render(
      <ExecutionTaskTray
        tasks={[{
          ...task,
          profile: "artifact.extended.v1",
          approval_state: "pending",
          approval_reason: "Create a larger staged image preview.",
          approval_expires_at: "2026-07-21T18:30:00Z",
          can_cancel: false,
        }]}
        onDecideApproval={onDecideApproval}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("1 background task requires approval.");
    expect(screen.getByText("Create a larger staged image preview.")).toBeVisible();
    expect(screen.getByText(/Action required · artifact extended/)).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("complementary").querySelector(".execution-task-spinner")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Stop background task/ })).not.toBeInTheDocument();

    const allow = screen.getByRole("button", { name: "Allow background task job-1 once" });
    const deny = screen.getByRole("button", { name: "Deny background task job-1" });
    await user.click(allow);
    expect(onDecideApproval).toHaveBeenCalledWith("job-1", "approved");
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
    finishDecision?.();
    await waitFor(() => expect(allow).toBeEnabled());
  });

  it("re-enables a pending approval after a handled request failure", async () => {
    const user = userEvent.setup();
    const onDecideApproval = vi.fn().mockRejectedValue(new Error("offline"));
    render(
      <ExecutionTaskTray
        tasks={[{ ...task, approval_state: "pending", can_cancel: false }]}
        onDecideApproval={onDecideApproval}
      />,
    );

    const deny = screen.getByRole("button", { name: "Deny background task job-1" });
    await user.click(deny);
    expect(onDecideApproval).toHaveBeenCalledWith("job-1", "denied");
    await waitFor(() => expect(deny).toBeEnabled());
    expect(screen.getByRole("status")).toHaveTextContent("requires approval");
  });
});
