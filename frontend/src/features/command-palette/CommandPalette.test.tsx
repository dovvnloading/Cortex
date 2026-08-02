import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ChatSummary } from "../../../../contracts/cortex-api";
import { useUiStore } from "../../stores/useUiStore";
import { CommandPalette } from "./CommandPalette";

const chats: ChatSummary[] = [
  { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" },
];

function renderPalette(overrides: Partial<Parameters<typeof CommandPalette>[0]> = {}) {
  const props = {
    chats,
    localModels: ["qwen3:8b", "granite4:tiny-h"],
    selectedModel: "qwen3:8b",
    onNewChat: vi.fn(),
    onOpenSettings: vi.fn(),
    onToggleTheme: vi.fn(),
    onSelectModel: vi.fn(),
    onSelectChat: vi.fn(),
    ...overrides,
  };
  render(<CommandPalette {...props} />);
  return props;
}

describe("CommandPalette", () => {
  it("is closed by default", () => {
    renderPalette();
    expect(screen.queryByPlaceholderText("Type a command or search chats…")).not.toBeInTheDocument();
  });

  it("opens on Ctrl+K and closes on a second press", async () => {
    renderPalette();
    const user = userEvent.setup();

    await user.keyboard("{Control>}k{/Control}");
    expect(await screen.findByPlaceholderText("Type a command or search chats…")).toBeVisible();

    await user.keyboard("{Control>}k{/Control}");
    expect(screen.queryByPlaceholderText("Type a command or search chats…")).not.toBeInTheDocument();
  });

  it("runs New chat and closes the palette", async () => {
    const props = renderPalette();
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.click(await screen.findByText("New chat"));

    expect(props.onNewChat).toHaveBeenCalledOnce();
    expect(screen.queryByPlaceholderText("Type a command or search chats…")).not.toBeInTheDocument();
  });

  it("lists installed models and marks the currently selected one", async () => {
    const props = renderPalette();
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    const currentModelItem = await screen.findByText("Switch to qwen3:8b");
    expect(currentModelItem.closest(".command-palette-item")).toHaveTextContent("Current");

    await user.click(screen.getByText("Switch to granite4:tiny-h"));
    expect(props.onSelectModel).toHaveBeenCalledWith("granite4:tiny-h");
  });

  it("lists recent chats and selects one", async () => {
    const props = renderPalette();
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.click(await screen.findByText("Quarterly planning"));
    expect(props.onSelectChat).toHaveBeenCalledWith("chat-1");
  });
});
