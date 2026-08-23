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
  source_digest: "b8a86ce3ec917cee7a187e89c6a0a8cad33e7df8f37e47c237e864a023f910e3",
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

  it("shows failed code runs as failed instead of complete and explains a timeout", () => {
    render(
      <ExecutionTaskTray
        tasks={[{
          ...codeTask,
          status: "failed",
          sequence: 6,
          message: "Local code execution failed safely.",
          error: "worker_timeout",
          result: null,
        }]}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("The latest local task failed.");
    expect(screen.getByText("Failed", { selector: ".execution-task-state-failed" })).toBeVisible();
    expect(screen.queryByText("Complete")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("timed out before returning output");
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
      source_digest: codeTask.source_digest,
      intent_summary: "Summarize the local data",
      capabilities: { filesystem: false, process: false, network: false },
    });
    render(<ExecutionTaskTray tasks={[codeTask]} onLoadCodeSource={onLoadCodeSource} />);

    expect(screen.getByText("Summarize the local data")).toBeVisible();
    expect(screen.getByText("No host access requested")).toBeVisible();
    expect(screen.getByText("Output")).toBeVisible();
    expect(screen.getByText("Rows: 3")).toBeVisible();

    await user.click(screen.getByText("Review generated source"));
    await waitFor(() => expect(onLoadCodeSource).toHaveBeenCalledWith("code-1"));
    expect(await screen.findByText("print('Rows: 3')")).toBeVisible();
  });

  it("requires a verified source review before approving code", async () => {
    const user = userEvent.setup();
    const onDecideApproval = vi.fn().mockResolvedValue(undefined);
    const onLoadCodeSource = vi.fn().mockResolvedValue({
      job_id: "code-1",
      language: "python",
      source: "print('Rows: 3')",
      source_digest: codeTask.source_digest,
      intent_summary: codeTask.intent_summary,
      capabilities: codeTask.capabilities,
    });
    render(
      <ExecutionTaskTray
        tasks={[{ ...codeTask, status: "queued", approval_state: "pending", result: null }]}
        onDecideApproval={onDecideApproval}
        onLoadCodeSource={onLoadCodeSource}
      />,
    );

    const allow = screen.getByRole("button", { name: "Allow background task code-1 once" });
    expect(allow).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deny background task code-1" })).toBeEnabled();
    expect(allow).toHaveAttribute("title", "Review and verify the generated source first.");

    await user.click(screen.getByText("Review generated source"));
    expect(await screen.findByText("print('Rows: 3')")).toBeVisible();
    await waitFor(() => expect(allow).toBeEnabled());
    await user.click(allow);
    expect(onDecideApproval).toHaveBeenCalledWith("code-1", "approved");
  });

  it("fails closed and offers retry when source review cannot be loaded", async () => {
    const user = userEvent.setup();
    const onLoadCodeSource = vi.fn().mockRejectedValue(new Error("offline"));
    render(
      <ExecutionTaskTray
        tasks={[{ ...codeTask, status: "queued", approval_state: "pending", result: null }]}
        onDecideApproval={vi.fn().mockResolvedValue(undefined)}
        onLoadCodeSource={onLoadCodeSource}
      />,
    );

    await user.click(screen.getByText("Review generated source"));

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be verified");
    expect(screen.getByRole("button", { name: "Allow background task code-1 once" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deny background task code-1" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(onLoadCodeSource).toHaveBeenCalledTimes(2));
  });

  it.each([
    {
      name: "digest",
      response: {
        job_id: "code-1",
        language: "python" as const,
        source: "print('Rows: 3')",
        source_digest: "0".repeat(64),
        intent_summary: codeTask.intent_summary,
        capabilities: codeTask.capabilities,
      },
    },
    {
      name: "capabilities",
      response: {
        job_id: "code-1",
        language: "python" as const,
        source: "print('Rows: 3')",
        source_digest: codeTask.source_digest,
        intent_summary: codeTask.intent_summary,
        capabilities: { filesystem: true, process: false, network: false },
      },
    },
    {
      name: "source bytes",
      response: {
        job_id: "code-1",
        language: "python" as const,
        source: "print('Rows: 4')",
        source_digest: codeTask.source_digest,
        intent_summary: codeTask.intent_summary,
        capabilities: codeTask.capabilities,
      },
    },
  ])("fails closed when reviewed source $name does not match the approved task", async ({ response }) => {
    const user = userEvent.setup();
    render(
      <ExecutionTaskTray
        tasks={[{ ...codeTask, status: "queued", approval_state: "pending", result: null }]}
        onDecideApproval={vi.fn().mockResolvedValue(undefined)}
        onLoadCodeSource={vi.fn().mockResolvedValue(response)}
      />,
    );

    await user.click(screen.getByText("Review generated source"));

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be verified");
    expect(screen.getByRole("button", { name: "Allow background task code-1 once" })).toBeDisabled();
    expect(screen.queryByText("print('Rows: 3')")).not.toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Allow background task code-1 once" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deny background task code-1" })).toBeEnabled();
  });
});
