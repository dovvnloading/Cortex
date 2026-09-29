import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatGroup, ExecutionTaskSummary } from "../../../../contracts/cortex-api";
import { readLastCrash } from "../../lib/crashLog";
import { AppShell } from "./AppShell";

// The chat list and the task tray both crash on render; the shell is real.
vi.mock("./ChatLibrary", () => ({
  ChatLibrary: () => {
    throw new Error("synthetic chat list crash");
  },
}));
vi.mock("./ExecutionTaskTray", () => ({
  ExecutionTaskTray: () => {
    throw new Error("synthetic task tray crash");
  },
}));

const task = { job_id: "task-1", status: "running" } as unknown as ExecutionTaskSummary;

function renderShell() {
  const onOpenSettings = vi.fn();
  render(
    <AppShell
      chats={[{ id: "chat-1", title: "A chat", timestamp: "2026-01-01T00:00:00Z" }]}
      groups={[] as ChatGroup[]}
      activeChatId={null}
      modelConnection={{ success: true, status: "connected", message: "Connected." }}
      theme="dark"
      executionTasks={[task]}
      onOpenSettings={onOpenSettings}
      onRenameChat={vi.fn().mockResolvedValue(undefined)}
      onDeleteChat={vi.fn().mockResolvedValue(undefined)}
      onCreateGroup={vi.fn().mockResolvedValue(undefined)}
      onRenameGroup={vi.fn().mockResolvedValue(undefined)}
      onDeleteGroup={vi.fn().mockResolvedValue(undefined)}
      onToggleGroup={vi.fn()}
      onMoveChat={vi.fn()}
    >
      <div>Chat content</div>
    </AppShell>,
  );
  return { onOpenSettings };
}

describe("AppShell widgets that crash", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("replaces the chat list and the task tray with short notes and keeps the rest working", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { onOpenSettings } = renderShell();
    const user = userEvent.setup();

    expect(screen.getByText("The chat list could not be shown.")).toBeVisible();
    expect(screen.getByText("The background task list could not be shown.")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(2);
    // Everything around them is still the working shell.
    expect(screen.getByText("Chat content")).toBeVisible();
    expect(screen.getByRole("button", { name: "New thread" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    // Whichever crashed last is on record, named by the part that broke.
    expect(["chat-library", "execution-tray"]).toContain(readLastCrash()?.scope);
  });
});
