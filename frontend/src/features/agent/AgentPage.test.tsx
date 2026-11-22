import { act, render, screen, waitFor, within } from "@testing-library/react";
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
  let journalAvailable = false;
  return {
    workspaceCheckpoints: vi.fn().mockResolvedValue({ checkpoints: [] }),
    startAgentRun: vi.fn().mockResolvedValue(run),
    agentRuns: vi.fn().mockResolvedValue({ runs: [] }),
    agentRun: vi.fn().mockImplementation(async () => ({
      ...run, status: "running", undo_available: journalAvailable,
    })),
    streamAgentRun: vi.fn().mockResolvedValue(undefined),
    decideAgentApproval: vi.fn().mockResolvedValue({ ...run, status: "running" }),
    resumeAgentRun: vi.fn().mockResolvedValue({ ...run, status: "completed" }),
    cancelAgentRun: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
    agentChangeset: vi.fn().mockImplementation(async (runId: string) => ({
      run_id: runId,
      changes: [{ path: "app.py", kind: "modify", baseline_sha256: "before", new_sha256: "after",
        diff: "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 42\n+value = 43\n" }],
    })),
    acceptAgentChanges: vi.fn().mockImplementation(async () => {
      journalAvailable = true;
      return { paths: ["app.py"] };
    }),
    discardAgentRun: vi.fn().mockResolvedValue({ discarded: true }),
    undoAgentChanges: vi.fn().mockImplementation(async () => {
      journalAvailable = false;
      return { paths: ["app.py"] };
    }),
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
    expect(screen.getByRole("region", { name: "Saved recovery checkpoints" })).toBeInTheDocument();
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
    expect(screen.getByRole("checkbox", { name: /I approve Cortex reading/i, hidden: true })).not.toBeChecked();
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

  it("refreshes run history when the active run finishes, without a manual Refresh", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    let listed: AgentRunResponse["status"] = "running";
    api.agentRuns.mockImplementation(async () => ({ runs: [{ ...run, status: listed }] }));
    api.startAgentRun.mockResolvedValue({ ...run, status: "running" });
    let emit: ((event: AgentSSEEvent) => void) | null = null;
    api.streamAgentRun.mockImplementation(async (_id: string, onEvent: (event: AgentSSEEvent) => void) => {
      emit = onEvent;
      return new Promise<void>(() => undefined);
    });
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const history = screen.getByRole("region", { name: "Agent run history" });
    expect(await within(history).findByText("Running")).toBeInTheDocument();
    await waitFor(() => expect(emit).not.toBeNull());

    listed = "completed";
    act(() => emit?.({ event_id: 1, run_id: "run-1", event: "status", data: { status: "completed" } }));

    expect(await within(history).findByText("Completed")).toBeInTheDocument();
    expect(within(history).queryByText("Running")).not.toBeInTheDocument();
  });

  it("loads the changeset and renders the merged review for a finished run", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByRole("heading", { name: "Review agent changes" })).toBeVisible();
    expect(within(dialog).getByRole("checkbox", { name: "Select app.py" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Accept selected to project" })).toBeEnabled();
    expect(api.agentChangeset).toHaveBeenCalledWith("run-1");
    expect(api.acceptAgentChanges).not.toHaveBeenCalled();
  });

  it("never accepts without a second confirmation or before the server acknowledges paths", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    let resolveAccept!: (value: { paths: string[] }) => void;
    api.acceptAgentChanges.mockImplementation(() => new Promise((resolve) => { resolveAccept = resolve; }));
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");

    await user.click(within(dialog).getByRole("button", { name: "Accept selected to project" }));
    expect(api.acceptAgentChanges).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.acceptAgentChanges).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Accept selected to project" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm accept" }));
    expect(api.acceptAgentChanges).toHaveBeenCalledWith("run-1", { paths: ["app.py"], confirmed: true });
    expect(within(dialog).getByText(/Waiting for the server to confirm/)).toBeVisible();
    expect(within(dialog).queryByText("Changes accepted to the project.")).not.toBeInTheDocument();

    resolveAccept({ paths: ["app.py"] });
    expect(await screen.findByText("Accepted to the project: app.py")).toBeVisible();
  });

  it("confirms discard, reports the server result, and never offers acceptance afterward", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");

    await user.click(within(dialog).getByRole("button", { name: "Discard all" }));
    expect(api.discardAgentRun).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm discard" }));
    await waitFor(() => expect(api.discardAgentRun).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByText("Work copy discarded. Discard did not alter the project.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Accept selected to project" })).not.toBeInTheDocument();
    expect(api.acceptAgentChanges).not.toHaveBeenCalled();
  });

  it("rechecks installation-owned legacy recovery after confirmed Agent accept and undo", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    let agentUndoAvailable = false;
    const legacyCheckpoint = {
      id: "legacy-checkpoint", root: "C:\\dev\\old-project", state: "pending",
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:01:00Z",
      paths: ["app.py"], file_count: 1,
    };
    // The saved-recovery endpoint lists installation-owned Project workspace journals.
    // Agent accepts use session-owned journals and have their own confirmed run undo.
    api.workspaceCheckpoints.mockResolvedValue({ checkpoints: [legacyCheckpoint] });
    api.agentRun.mockImplementation(async () => ({
      ...run, undo_available: agentUndoAvailable,
    }));
    api.acceptAgentChanges.mockImplementation(async () => {
      agentUndoAvailable = true;
      return { paths: ["app.py"] };
    });
    api.undoAgentChanges.mockImplementation(async () => {
      agentUndoAvailable = false;
      return { paths: ["app.py"] };
    });

    page(api);
    const recovery = screen.getByRole("region", { name: "Saved recovery checkpoints" });
    expect(await within(recovery).findByText(legacyCheckpoint.root)).toBeVisible();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(1);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const review = await screen.findByRole("alertdialog", { name: "Review agent changes" });

    await user.click(within(review).getByRole("button", { name: "Accept selected to project" }));
    await user.click(within(review).getByRole("button", { name: "Confirm accept" }));
    expect(await screen.findByText("Accepted to the project: app.py")).toBeVisible();
    await waitFor(() => expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(2));
    expect(await within(recovery).findByText(legacyCheckpoint.root)).toBeVisible();
    expect(within(recovery).queryByText(run.project_root)).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Undo" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
    await waitFor(() => expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(3));
    expect(await within(recovery).findByText(legacyCheckpoint.root)).toBeVisible();
    expect(within(recovery).queryByText("No saved recovery checkpoints.")).not.toBeInTheDocument();
  });

  it("requires a fresh confirmation before undo, then shows restored paths", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Accept selected to project" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm accept" }));
    await screen.findByText("Accepted to the project: app.py");

    await user.click(await screen.findByRole("button", { name: "Undo" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    expect(screen.getByRole("group", { name: "Undo confirmation" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    await waitFor(() => expect(api.undoAgentChanges).toHaveBeenCalledWith("run-1", { confirmed: true }));
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
    expect(await screen.findByRole("button", { name: "Accept selected to project" })).toBeEnabled();
  });

  it("keeps the review open and reports a rejected accept without success", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.acceptAgentChanges.mockRejectedValueOnce(new Error("network unavailable"));
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Accept selected to project" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm accept" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not confirm acceptance");
    expect(within(dialog).queryByText("Changes accepted to the project.")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Accept selected to project" })).toBeEnabled();
  });

  it("opens a selected past run from history and follows its events", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    const old = { ...run, id: "historical-run", status: "running", project_root: "C:\\\\archive\\\\example" };
    api.agentRuns.mockResolvedValue({ runs: [old] });
    api.agentRun.mockResolvedValue(old);

    let onStreamEvent!: (event: AgentSSEEvent) => void;
    let finishStream!: () => void;
    let notifySubscribed!: () => void;
    const subscribed = new Promise<void>((resolve) => { notifySubscribed = resolve; });
    api.streamAgentRun.mockImplementation((_id, onEvent) => {
      onStreamEvent = onEvent;
      notifySubscribed();
      return new Promise<void>((resolve) => { finishStream = resolve; });
    });
    page(api);

    await user.click(await screen.findByRole("button", { name: "Open run: Goal unavailable" }));
    // The effect subscribes only after the selected run is committed to the page.
    await subscribed;
    expect(api.agentRun).toHaveBeenCalledWith("historical-run");
    expect(api.streamAgentRun).toHaveBeenCalledWith(
      "historical-run", expect.any(Function), expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole("heading", { name: "Working on your goal" })).toBeVisible();

    await act(async () => {
      onStreamEvent({ event_id: 1, run_id: "historical-run", event: "step",
        data: { seq: 1, kind: "tool", tool: "read_file", summary: "Read archive files" } });
      onStreamEvent({ event_id: 2, run_id: "historical-run", event: "status", data: { status: "completed" } });
      finishStream();
    });
    // The terminal review is modal, so close it before querying the background heading.
    await screen.findByRole("alertdialog", { name: "Review agent changes" });
    await user.keyboard("{Escape}");
    expect(await screen.findByRole("button", { name: "Reopen changes review" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Run completed" })).toBeVisible();
    expect(within(screen.getByRole("list", { name: "Agent activity" }))
      .getByText("Read archive files")).toBeVisible();
    await waitFor(() => expect(api.agentChangeset).toHaveBeenCalledWith("historical-run"));
  });

  it("shows server-backed Undo for historical partially accepted runs with pending files", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    const historical = { ...run, id: "historical-partial" };
    let journalAvailable = true;
    api.agentRuns.mockResolvedValue({ runs: [historical] });
    api.agentRun.mockImplementation(async () => ({
      ...historical, undo_available: journalAvailable,
    }));
    api.agentChangeset.mockResolvedValue({
      run_id: historical.id,
      changes: [
        { path: "pending.py", kind: "add", baseline_sha256: null, new_sha256: "after", diff: "+pending = True" },
      ],
    });
    api.undoAgentChanges.mockImplementation(async () => {
      journalAvailable = false;
      return { paths: ["app.py"] };
    });
    page(api);
    await user.click(await screen.findByRole("button", { name: "Open run: Goal unavailable" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Review agent changes" });
    expect(within(dialog).getByRole("checkbox", { name: "Select pending.py" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Accept selected to project" })).toBeEnabled();
    const undo = await within(dialog).findByRole("button", { name: "Undo" });
    expect(api.undoAgentChanges).not.toHaveBeenCalled();

    await user.click(undo);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Undo" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm undo" }));
    await waitFor(() => expect(api.undoAgentChanges).toHaveBeenCalledWith(
      "historical-partial", { confirmed: true },
    ));
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument());
    expect(api.acceptAgentChanges).not.toHaveBeenCalled();
  });

  it("keeps journal Undo after confirmed Discard, requiring another confirmation and server ack", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    const pending = {
      path: "pending.py", kind: "add", baseline_sha256: null,
      new_sha256: "pending", diff: "+pending = True",
    };
    api.agentChangeset.mockImplementation(async (runId: string) => {
      // The accepted file drops out, but this second change remains staged
      // until Discard destroys the disposable copy.
      if (api.discardAgentRun.mock.calls.length > 0) throw new Error("copy unavailable");
      const changes = api.acceptAgentChanges.mock.calls.length > 0 ? [pending] : [
        { path: "app.py", kind: "modify", baseline_sha256: "before", new_sha256: "after",
          diff: "-value = 42\\n+value = 43" },
        pending,
      ];
      return { run_id: runId, changes };
    });
    let resolveUndo!: (value: { paths: string[] }) => void;
    api.undoAgentChanges.mockImplementation(() => new Promise((resolve) => { resolveUndo = resolve; }));
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    await screen.findByRole("alertdialog");
    await user.click(screen.getByRole("checkbox", { name: "Select pending.py" }));
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    await user.click(screen.getByRole("button", { name: "Confirm accept" }));
    await screen.findByText("Accepted to the project: app.py");

    expect(await screen.findByRole("checkbox", { name: "Select pending.py" })).toBeChecked();
    const undo = await screen.findByRole("button", { name: "Undo" });
    expect(undo).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    expect(api.discardAgentRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm discard" }));
    await waitFor(() => expect(api.discardAgentRun).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByText("Work copy discarded. Discard did not alter the project.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Accept selected to project" })).not.toBeInTheDocument();
    const recover = await screen.findByRole("button", { name: "Undo previously accepted changes" });
    await user.click(recover);
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo previously accepted changes" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    expect(api.undoAgentChanges).toHaveBeenCalledWith("run-1", { confirmed: true });
    expect(screen.getByText("Waiting for the server to confirm the action…")).toBeVisible();
    expect(screen.queryByText("Restored original project files: app.py")).not.toBeInTheDocument();
    resolveUndo({ paths: ["app.py"] });
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Undo previously accepted changes" })).not.toBeInTheDocument();
  });

  it("does not offer journal Undo when the run reports no checkpoint", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.agentChangeset.mockResolvedValue({ run_id: "run-1", changes: [] });
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("No files to review.")).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: /Undo/ })).not.toBeInTheDocument();
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
  });

  it("offers separately confirmed recovery when a past work copy is gone", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.agentRuns.mockResolvedValue({ runs: [{ ...run, id: "discarded-run" }] });
    api.agentRun.mockResolvedValue({ ...run, id: "discarded-run", undo_available: true });
    api.agentChangeset.mockRejectedValue(new Error("copy unavailable"));
    page(api);

    await user.click(await screen.findByRole("button", { name: "Open run: Goal unavailable" }));
    expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent("Could not load the staged changes");
    await user.click(screen.getByRole("button", { name: "Undo previously accepted changes" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo previously accepted changes" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    await waitFor(() => expect(api.undoAgentChanges).toHaveBeenCalledWith("discarded-run", { confirmed: true }));
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Undo previously accepted changes" })).not.toBeInTheDocument();
  });

  it("offers confirmed recovery without asserting prior acceptance for an empty changeset", async () => {
    const user = userEvent.setup();
    const api = fakeClient();
    api.agentChangeset.mockResolvedValue({ run_id: "run-1", changes: [] });
    api.agentRun.mockResolvedValue({ ...run, undo_available: true });
    page(api);
    await prepare(user);
    await user.click(screen.getByRole("button", { name: "Start Agent" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("No files to review.")).toBeVisible();
    expect(within(dialog).queryByText("Changes accepted to the project.")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Undo previously accepted changes" }));
    expect(api.undoAgentChanges).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm undo" }));
    await waitFor(() => expect(api.undoAgentChanges).toHaveBeenCalledWith("run-1", { confirmed: true }));
    expect(await screen.findByText("Restored original project files: app.py")).toBeVisible();
  });

});
