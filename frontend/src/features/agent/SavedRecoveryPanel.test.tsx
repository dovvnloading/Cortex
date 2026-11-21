import { StrictMode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CortexApi } from "../../api/client";
import { SavedRecoveryPanel } from "./SavedRecoveryPanel";

const checkpoint = {
  id: "legacy-checkpoint", root: "C:\\projects\\legacy",
  state: "pending" as const, created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:05:00Z", paths: ["app.py"], file_count: 1,
};

function fakeApi() {
  return {
    workspaceCheckpoints: vi.fn().mockResolvedValue({ checkpoints: [checkpoint] }),
    restoreWorkspaceCheckpoint: vi.fn().mockResolvedValue({
      status: "restored", checkpoint_id: checkpoint.id, paths: ["app.py"],
    }),
    discardWorkspaceCheckpoint: vi.fn().mockResolvedValue({ discarded: true }),
    startAgentRun: vi.fn(),
  };
}

function mount(api: ReturnType<typeof fakeApi>) {
  return render(<SavedRecoveryPanel api={api as unknown as CortexApi} />);
}

describe("saved recovery checkpoints in Agent mode", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads pending legacy checkpoints without starting a run or restoring files", async () => {
    const api = fakeApi();
    mount(api);
    expect(await screen.findByText(checkpoint.root)).toBeInTheDocument();
    expect(screen.getByText(/Interrupted\/uncertain write/)).toBeInTheDocument();
    expect(screen.getByText("app.py")).toBeInTheDocument();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(1);
    expect(api.startAgentRun).not.toHaveBeenCalled();
    expect(api.restoreWorkspaceCheckpoint).not.toHaveBeenCalled();
    expect(api.discardWorkspaceCheckpoint).not.toHaveBeenCalled();
  });

  it("reloads on a new Agent recovery revision and ignores older responses", async () => {
    const api = fakeApi();
    let finishStale!: (value: { checkpoints: (typeof checkpoint)[] }) => void;
    api.workspaceCheckpoints.mockResolvedValueOnce({ checkpoints: [] })
      .mockImplementationOnce(() => new Promise((resolve) => { finishStale = resolve; }))
      .mockResolvedValueOnce({ checkpoints: [checkpoint] });

    const { rerender } = mount(api);
    expect(await screen.findByText("No saved recovery checkpoints.")).toBeInTheDocument();
    rerender(<SavedRecoveryPanel api={api as unknown as CortexApi} refreshKey={1} />);
    await waitFor(() => expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Checking for saved recovery checkpoints…")).toBeInTheDocument();
    // A second confirmed mutation supersedes a still-unanswered journal read.
    rerender(<SavedRecoveryPanel api={api as unknown as CortexApi} refreshKey={2} />);
    expect(await screen.findByText(checkpoint.root)).toBeInTheDocument();
    await act(async () => finishStale({ checkpoints: [] }));
    expect(screen.getByText(checkpoint.root)).toBeInTheDocument();
    expect(screen.queryByText("No saved recovery checkpoints.")).not.toBeInTheDocument();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(3);
  });

  it("does not launch a stale initial fetch after StrictMode effect cleanup", async () => {
    const api = fakeApi();
    render(
      <StrictMode>
        <SavedRecoveryPanel api={api as unknown as CortexApi} />
      </StrictMode>,
    );
    expect(await screen.findByText(checkpoint.root)).toBeInTheDocument();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(1);
  });

  it("restores only after separate confirmation, then refreshes the list", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.workspaceCheckpoints.mockResolvedValueOnce({ checkpoints: [checkpoint] })
      .mockResolvedValue({ checkpoints: [] });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount(api);
    await screen.findByText(checkpoint.root);
    await user.click(screen.getByRole("button", { name: "Restore originals" }));
    expect(api.restoreWorkspaceCheckpoint).not.toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("verify ALL current file hashes"));

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Restore originals" }));
    await waitFor(() => expect(api.restoreWorkspaceCheckpoint).toHaveBeenCalledWith(checkpoint.id));
    expect(await screen.findByText("No saved recovery checkpoints.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Restored 1 source files");
    expect(api.startAgentRun).not.toHaveBeenCalled();
  });

  it("discards a saved backup only after confirmation, never restoring source", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.workspaceCheckpoints.mockResolvedValueOnce({ checkpoints: [checkpoint] })
      .mockResolvedValue({ checkpoints: [] });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount(api);
    await screen.findByText(checkpoint.root);
    await user.click(screen.getByRole("button", { name: "Discard backup" }));
    expect(api.discardWorkspaceCheckpoint).not.toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("CANNOT be restored afterward"));

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Discard backup" }));
    await waitFor(() => expect(api.discardWorkspaceCheckpoint).toHaveBeenCalledWith(checkpoint.id));
    expect(await screen.findByText("No saved recovery checkpoints.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Source files were not changed.");
    expect(api.restoreWorkspaceCheckpoint).not.toHaveBeenCalled();
  });

  it("keeps recovery choices available when a source hash conflicts", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.restoreWorkspaceCheckpoint.mockRejectedValue(new Error("stale_source"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(api);
    await screen.findByText(checkpoint.root);
    await user.click(screen.getByRole("button", { name: "Restore originals" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("saved recovery operation failed");
    expect(screen.getByRole("button", { name: "Restore originals" })).toBeEnabled();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(1);
    expect(api.discardWorkspaceCheckpoint).not.toHaveBeenCalled();
  });

  it("never mistakes an unavailable journal for an empty one and can retry", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.workspaceCheckpoints.mockRejectedValueOnce(new Error("journal not readable"))
      .mockResolvedValueOnce({ checkpoints: [] });
    mount(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("Saved recovery copies could not be checked");
    expect(screen.queryByText("No saved recovery checkpoints.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh recovery list" }));
    expect(await screen.findByText("No saved recovery checkpoints.")).toBeInTheDocument();
  });

  it("disables competing recovery actions while a restoration is pending", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    let finish!: (result: { status: string; checkpoint_id: string; paths: string[] }) => void;
    api.restoreWorkspaceCheckpoint.mockImplementationOnce(() =>
      new Promise((resolve) => { finish = resolve; }));
    api.workspaceCheckpoints.mockResolvedValueOnce({ checkpoints: [checkpoint] })
      .mockResolvedValueOnce({ checkpoints: [] });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(api);
    await screen.findByText(checkpoint.root);
    await user.click(screen.getByRole("button", { name: "Restore originals" }));
    expect(screen.getByRole("button", { name: "Restore originals" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discard backup" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Refresh recovery list" })).toBeDisabled();
    expect(api.restoreWorkspaceCheckpoint).toHaveBeenCalledTimes(1);
    await act(async () => {
      finish({ status: "restored", checkpoint_id: checkpoint.id, paths: ["app.py"] });
    });
    expect(await screen.findByText("No saved recovery checkpoints.")).toBeInTheDocument();
  });

  it("does not report success when a restore or discard lacks server acknowledgement", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.restoreWorkspaceCheckpoint.mockResolvedValueOnce({
      status: "restored", checkpoint_id: "other-checkpoint", paths: ["app.py"],
    });
    api.discardWorkspaceCheckpoint.mockResolvedValueOnce({ discarded: false });
    mount(api);
    await screen.findByText(checkpoint.root);
    await user.click(screen.getByRole("button", { name: "Restore originals" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("saved recovery operation failed");
    expect(screen.queryByText(/Restored 1 source files/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard backup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("saved recovery operation failed");
    expect(screen.queryByText(/Source files were not changed/)).not.toBeInTheDocument();
    expect(api.workspaceCheckpoints).toHaveBeenCalledTimes(1);
  });
});
