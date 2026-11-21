import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentRunResponse, AgentSSEEvent } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { AgentPage } from "./AgentPage";

const run: AgentRunResponse = {
  id: "run-1", project_root: "C:\\dev\\sample", mode: "plan_only",
  model: "qwen3:8b", status: "completed", budgets: {},
  created: "2026-01-01", updated: "2026-01-01",
};

function fakeClient() {
  return {
    startAgentRun: vi.fn().mockResolvedValue(run),
    agentRuns: vi.fn().mockResolvedValue({ runs: [] }),
    agentRun: vi.fn().mockResolvedValue({ ...run, status: "running" }),
    streamAgentRun: vi.fn().mockResolvedValue(undefined),
    decideAgentApproval: vi.fn().mockResolvedValue({ ...run, status: "running" }),
    resumeAgentRun: vi.fn().mockResolvedValue({ ...run, status: "completed" }),
    cancelAgentRun: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
  };
}

function page(api = fakeClient(), enabled = true) {
  return render(<AgentPage
    api={api as unknown as CortexApi}
    enabled={enabled}
    localModels={["qwen3:8b"]}
    cloudModels={["cloud:openai:gpt-4o"]}
    selectedModel="qwen3:8b"
  />);
}

async function prepare(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Explain this project" }));
  await user.type(screen.getByRole("textbox", { name: "Project folder" }), "C:\\dev\\sample");
  await user.click(screen.getByRole("checkbox", { name: /I approve Cortex reading and copying/i }));
}

describe("AgentPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("explains the disabled default and directs the user to Settings", () => {
    const api = fakeClient();
    page(api, false);
    expect(screen.getByRole("heading", { name: "Agent mode is off" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.queryByRole("button", { name: "Start Agent" })).not.toBeInTheDocument();
    expect(api.startAgentRun).not.toHaveBeenCalled();
  });

  it("requires consent for the selected project and resets it when the folder changes", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    page(api);
    const start = screen.getByRole("button", { name: "Start Agent" });
    await user.click(screen.getByRole("button", { name: "Fix failing tests" }));
    await user.type(screen.getByRole("textbox", { name: "Project folder" }), "C:\\dev\\sample");
    expect(start).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /I approve Cortex reading and copying/i }));
    expect(start).toBeEnabled();
    await user.type(screen.getByRole("textbox", { name: "Project folder" }), "2");
    expect(start).toBeDisabled();
    expect(api.startAgentRun).not.toHaveBeenCalled();
  });

  it("blocks cloud-model starts until the provider warning is explicitly confirmed", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    page(api);
    await prepare(user);
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "cloud:openai:gpt-4o");
    expect(screen.getByText(/Your files will be sent to openai/)).toBeVisible();
    const start = screen.getByRole("button", { name: "Start Agent" });
    expect(start).toBeDisabled();
    expect(api.startAgentRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox", { name: /I understand and explicitly approve sending project context/i }));
    await user.click(start);
    await waitFor(() => expect(api.startAgentRun).toHaveBeenCalledWith(expect.objectContaining({
      project_root: "C:\\dev\\sample",
      model: "cloud:openai:gpt-4o",
      confirmed_project: true,
      confirmed_cloud: true,
    })));
    expect(screen.getByRole("checkbox", { name: /I approve Cortex reading/i })).not.toBeChecked();
  });

  it("uses the existing ApprovalCard and posts only a one-time human decision before resuming", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.startAgentRun.mockResolvedValue({ ...run, status: "running" });
    api.streamAgentRun.mockImplementation(async (_id: string, onEvent: (event: AgentSSEEvent) => void) => {
      onEvent({
        event_id: 1, run_id: "run-1", event: "approval",
        data: { seq: 3, request: { cls: "inspect", sandbox_level: "appcontainer", network: false, command: "echo hello", plain_text: "Review the command." } },
      });
      onEvent({ event_id: 2, run_id: "run-1", event: "status", data: { status: "waiting_approval" } });
    });
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Review the command.")).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "Always allow in this project" })).not.toBeInTheDocument();
    expect(api.decideAgentApproval).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(api.decideAgentApproval).toHaveBeenCalledWith("run-1", 3, "allow_once"));
    await waitFor(() => expect(api.resumeAgentRun).toHaveBeenCalledWith("run-1"));
  });

  it("stops through the cancel endpoint", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.startAgentRun.mockResolvedValue({ ...run, status: "running" });
    api.streamAgentRun.mockImplementation(async () => new Promise<void>(() => undefined));
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(api.cancelAgentRun).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByText("Run stopped")).toBeInTheDocument();
  });
});
