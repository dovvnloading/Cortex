import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspacePage } from "./WorkspacePage";
import type { CortexApi } from "../../api/client";

function fakeApi() {
  return {
    openWorkspace: vi.fn().mockResolvedValue({
      session_id: "snapshot-1", root: "/tmp/project", file_count: 1,
      total_bytes: 42, skipped_entries: 2, truncated: false,
      expires_at: "2030-01-01T00:15:00+00:00", state: "open",
    }),
    workspacePaths: vi.fn().mockResolvedValue({ paths: ["app.py"] }),
    workspaceSearch: vi.fn().mockResolvedValue({ matches: [{ path: "app.py", line: 1, text: "return 42" }] }),
    workspaceRead: vi.fn().mockResolvedValue({
      path: "app.py", start_line: 1, end_line: 2, total_lines: 2,
      sha256: "a".repeat(64), text: "1: def answer():\n2:     return 42",
      truncated: false,
    }),
    workspaceAsk: vi.fn().mockResolvedValue({
      status: "completed", answer: "The bug is in app.py:2.",
      model_calls: 3, tool_calls: 2, preview_diff:
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def answer():\n-    return 42\n+    return 43\n",
      preview_paths: ["app.py"], preview_replacements: 1, applied_to_checkout: false,
    }),
    workspaceApply: vi.fn().mockResolvedValue({
      status: "applied", paths: ["app.py"], undo_available: true,
    }),
    workspaceUndo: vi.fn().mockResolvedValue({
      status: "undone", paths: ["app.py"], undo_available: false,
    }),
    workspaceCheck: vi.fn().mockResolvedValue({
      status: "failed", command: "pytest", exit_code: 1,
      output: "AssertionError: expected 43", output_truncated: false, duration_seconds: 1.2,
    }),
    workspaceCancel: vi.fn().mockResolvedValue({ cancelled: true }),
    closeWorkspace: vi.fn().mockResolvedValue({ revoked: true }),
  };
}

async function openProject(user: ReturnType<typeof userEvent.setup>, api: ReturnType<typeof fakeApi>) {
  await user.type(screen.getByRole("textbox", { name: "Project folder" }), "/tmp/project");
  expect(api.openWorkspace).not.toHaveBeenCalled();
  await user.click(screen.getByRole("checkbox", { name: /I approve reading eligible source files/i }));
  await user.click(screen.getByRole("button", { name: "Open project" }));
  await waitFor(() => expect(api.openWorkspace).toHaveBeenCalledWith("/tmp/project"));
  expect(await screen.findByRole("heading", { name: "Source files" })).toBeInTheDocument();
}

describe("WorkspacePage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("never indexes a path until consent is explicitly checked", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    render(<WorkspacePage api={api as unknown as CortexApi} localModels={["qwen3:8b"]} selectedModel="qwen3:8b" />);
    const open = screen.getByRole("button", { name: "Open project" });
    expect(open).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Project folder" }), "/tmp/project");
    expect(open).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /I approve reading eligible source files/i }));
    expect(open).toBeEnabled();
    await user.clear(screen.getByRole("textbox", { name: "Project folder" }));
    expect(open).toBeDisabled();
    expect(api.openWorkspace).not.toHaveBeenCalled();
  });

  it("previews before applying, and requires separate approval for apply and undo", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<WorkspacePage api={api as unknown as CortexApi} localModels={["qwen3:8b"]} selectedModel="qwen3:8b" />);
    await openProject(user, api);
    await user.click(screen.getByRole("button", { name: "app.py" }));
    expect(await screen.findByText(/1: def answer/)).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Question for local model" }), "Fix the bug.");
    await user.click(screen.getByRole("button", { name: "Research project" }));
    const diff = await screen.findByRole("region", { name: "Side-by-side patch preview" });
    expect(diff).toHaveTextContent("return 42");
    expect(diff).toHaveTextContent("return 43");
    expect(api.workspaceApply).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Review & apply patch" }));
    expect(confirm).toHaveBeenCalled();
    expect(api.workspaceApply).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Review & apply patch" }));
    await waitFor(() => expect(api.workspaceApply).toHaveBeenCalledWith("snapshot-1"));
    expect(await screen.findByRole("button", { name: "Undo applied patch" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Undo applied patch" }));
    await waitFor(() => expect(api.workspaceUndo).toHaveBeenCalledWith("snapshot-1"));
    expect(screen.queryByRole("button", { name: "Undo applied patch" })).not.toBeInTheDocument();
  });

  it("gates running a fixed test command and displays failure output", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<WorkspacePage api={api as unknown as CortexApi} localModels={["qwen3:8b"]} selectedModel="qwen3:8b" />);
    await openProject(user, api);
    await user.click(screen.getByRole("button", { name: "Run approved check" }));
    expect(confirm).toHaveBeenCalled();
    expect(api.workspaceCheck).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Run approved check" }));
    await waitFor(() => expect(api.workspaceCheck).toHaveBeenCalledWith("snapshot-1", "pytest"));
    expect(await screen.findByText(/AssertionError: expected 43/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ask model to investigate failure" }));
    expect(screen.getByRole("textbox", { name: "Question for local model" })).toHaveValue(
      "Investigate this test failure and propose a fix:\nAssertionError: expected 43",
    );
  });
});
