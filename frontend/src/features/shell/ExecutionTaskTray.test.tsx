import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExecutionTaskSummary } from "../../../../contracts/cortex-api";
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

const codeTask: ExecutionTaskSummary = {
  ...task,
  job_id: "code-1",
  profile: "code.exec.v1",
  status: "succeeded",
  phase: "completed",
  message: "Local code execution completed.",
  intent_summary: "Summarize the local data",
  capabilities: { filesystem: false, process: false, network: false },
  result: { stdout: "Rows: 3\n", stderr: "", value: { rows: 3 }, truncated: false },
  can_cancel: false,
};

describe("ExecutionTaskTray", () => {
  it("announces active work and exposes an accessible Stop action", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn<(jobId: string) => Promise<void>>().mockResolvedValue();
    render(<ExecutionTaskTray tasks={[task]} onCancel={onCancel} />);

    expect(screen.getByRole("complementary", { name: "Task activity" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1 local task is running.");
    expect(screen.getByRole("complementary").querySelector(".execution-task-spinner")).toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "Stop background task job-1" });
    expect(stop).toBeEnabled();
    await user.click(stop);
    expect(onCancel).toHaveBeenCalledWith("job-1");
  });

  it("keeps terminal state visible without offering Stop", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ExecutionTaskTray tasks={[{ ...task, status: "succeeded", can_cancel: false, message: "Complete" }]} />);
    expect(screen.getByRole("status")).toHaveTextContent("The latest local task is complete.");
    expect(screen.queryByRole("button", { name: /Stop background task/ })).not.toBeInTheDocument();
    const dismiss = screen.getByRole("button", { name: "Dismiss completed background task notification" });
    expect(dismiss).toHaveAttribute("title", "Dismiss notification");
    await user.click(dismiss);
    expect(screen.queryByRole("complementary", { name: "Background tasks" })).not.toBeInTheDocument();

    rerender(<ExecutionTaskTray tasks={[{ ...task, job_id: "job-2", status: "succeeded", can_cancel: false, message: "Another complete task" }]} />);
    expect(screen.getByRole("button", { name: "Dismiss completed background task notification" })).toBeVisible();
  });

  it("groups repeated completed task notifications into one row", () => {
    render(
      <ExecutionTaskTray
        tasks={[
          { ...task, status: "succeeded", can_cancel: false, message: "Chat attachment staged." },
          { ...task, job_id: "job-2", status: "succeeded", can_cancel: false, message: "Chat attachment staged." },
        ]}
      />,
    );

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("2 × Chat attachment staged.")).toBeVisible();
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

    expect(screen.getByRole("status")).toHaveTextContent("1 task requires your approval.");
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
    expect(screen.getByRole("status")).toHaveTextContent("requires your approval");
  });

  it("renders a code result as a structured task card and loads source on demand", async () => {
    const user = userEvent.setup();
    const onLoadCodeSource = vi.fn().mockResolvedValue({
      job_id: "code-1",
      language: "python",
      source: "print('Rows: 3')",
      source_digest: "a".repeat(64),
      intent_summary: "Summarize the local data",
      capabilities: { filesystem: false, process: false, network: false },
    });
    render(<ExecutionTaskTray tasks={[codeTask]} onLoadCodeSource={onLoadCodeSource} />);

    expect(screen.getByText("Summarize the local data")).toBeVisible();
    expect(screen.getByText("No host access requested")).toBeVisible();
    expect(screen.getByText("Output")).toBeVisible();
    expect(screen.getByText("Rows: 3")).toBeVisible();

    await user.click(screen.getByText("Inspect generated source"));
    await waitFor(() => expect(onLoadCodeSource).toHaveBeenCalledWith("code-1"));
    expect(await screen.findByText("print('Rows: 3')")).toBeVisible();
  });

  it("makes broad code access explicit before approval", () => {
    render(
      <ExecutionTaskTray
        tasks={[{ ...codeTask, status: "queued", approval_state: "pending", approval_reason: "Prepare a local report", capabilities: { filesystem: true, process: true, network: false }, result: null }]}
        onDecideApproval={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByText("Broad local access requested. Review the source before allowing.")).toBeVisible();
    expect(screen.getByText("Files")).toBeVisible();
    expect(screen.getByText("Processes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Allow background task code-1 once" })).toBeVisible();
  });
});
