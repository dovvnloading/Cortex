import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    theme: "dark" as const,
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

/** Newest first, as the chat list arrives: "Topic 01" is the most recent. */
const manyChats = (count: number, title = (n: number) => `Topic ${String(n).padStart(2, "0")}`): ChatSummary[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `chat-${index + 1}`,
    title: title(index + 1),
    timestamp: "2026-01-01T00:00:00Z",
  }));

const chatRows = () => Array.from(document.querySelectorAll('[cmdk-item][data-value^="chat:"]'));

describe("CommandPalette chat search", () => {
  it("lists only the eight newest chats until something is typed", async () => {
    renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);

    expect(await screen.findByText("Recent chats")).toBeVisible();
    expect(chatRows()).toHaveLength(8);
    expect(screen.getByText("Topic 08")).toBeVisible();
    expect(screen.queryByText("Topic 09")).not.toBeInTheDocument();
  });

  it("finds a chat older than the recent eight by typing its title", async () => {
    const props = renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "Topic 12");

    // The heading follows the mode: the list is no longer "recent".
    expect(await screen.findByText("Topic 12")).toBeVisible();
    expect(screen.getByText("Chats")).toBeVisible();
    expect(screen.queryByText("Recent chats")).not.toBeInTheDocument();
    await user.click(screen.getByText("Topic 12"));
    expect(props.onSelectChat).toHaveBeenCalledWith("chat-12");
  });

  it("puts the best match first, not the newest", async () => {
    renderPalette({
      chats: [
        { id: "recent-weak", title: "Notes on the weekly review", timestamp: "2026-01-02T00:00:00Z" },
        { id: "older-exact", title: "Review", timestamp: "2026-01-01T00:00:00Z" },
      ],
    });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "Review");

    await screen.findByText("Review");
    const titles = chatRows().map((row) => row.textContent);
    expect(titles).toEqual(["Review", "Notes on the weekly review"]);
  });

  it("shows at most fifty results however many chats match", async () => {
    renderPalette({ chats: manyChats(400, (n) => `Planning notes ${n}`) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "Planning");

    await waitFor(() => expect(chatRows().length).toBeGreaterThan(8));
    expect(chatRows()).toHaveLength(50);
  });

  it("does not search on every keystroke", async () => {
    // Scoring every title for each key is what a long history cannot afford; the
    // rows on screen are for the last query that paused, not for each letter.
    renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const input = await screen.findByPlaceholderText("Type a command or search chats…");

    // One change event and then synchronous assertions: no timer can run in
    // between, so this is "straight after typing" exactly, on any machine.
    fireEvent.change(input, { target: { value: "Topic 12" } });

    // The older chat has not been searched for yet ...
    expect(screen.queryByText("Topic 12")).not.toBeInTheDocument();
    // ... and no premature "No results." is shown while it catches up.
    expect(screen.queryByText("No results.")).not.toBeInTheDocument();
    // Once typing has paused, the search runs.
    expect(await screen.findByText("Topic 12")).toBeVisible();
  });

  it("says there are no results once the search has settled on nothing", async () => {
    renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "zzzzqq");

    expect(await screen.findByText("No results.")).toBeVisible();
  });

  it("returns to the recent chats as soon as the box is cleared", async () => {
    renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText("Type a command or search chats…");
    await user.type(input, "Topic 12");
    await screen.findByText("Chats");

    await user.clear(input);

    expect(screen.getByText("Recent chats")).toBeVisible();
    expect(chatRows()).toHaveLength(8);
  });

  it("forgets the search when the palette closes", async () => {
    renderPalette({ chats: manyChats(12) });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();
    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "Topic 12");
    await screen.findByText("Chats");

    act(() => useUiStore.getState().setCommandPaletteOpen(false));
    expect(screen.queryByPlaceholderText("Type a command or search chats…")).not.toBeInTheDocument();
    act(() => useUiStore.getState().setCommandPaletteOpen(true));

    const reopened = await screen.findByPlaceholderText("Type a command or search chats…");
    expect(reopened).toHaveValue("");
    expect(screen.getByText("Recent chats")).toBeVisible();
  });

  it("keeps two chats with the same title apart, each reachable and selectable", async () => {
    const props = renderPalette({
      chats: [
        { id: "chat-a", title: "New Chat", timestamp: "2026-01-02T00:00:00Z" },
        { id: "chat-b", title: "New Chat", timestamp: "2026-01-01T00:00:00Z" },
      ],
    });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();
    await screen.findByText("Recent chats");
    const rows = chatRows();
    expect(rows).toHaveLength(2);

    // Pointing at the second highlights that row alone; before, both shared one value.
    await user.hover(rows[1]);
    expect(rows.map((row) => row.getAttribute("aria-selected"))).toEqual(["false", "true"]);

    await user.click(rows[1]);
    expect(props.onSelectChat).toHaveBeenCalledWith("chat-b");
  });

  it("matches a chat by its title, never by its id", async () => {
    renderPalette({ chats: [{ id: "aabbccdd-1122-3344", title: "Budget", timestamp: "2026-01-01T00:00:00Z" }] });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), "aabbcc");

    expect(await screen.findByText("No results.")).toBeVisible();
    expect(screen.queryByText("Budget")).not.toBeInTheDocument();
  });
});

describe("CommandPalette theme item", () => {
  it.each([
    ["system", "Next: Light"],
    ["light", "Next: Dark"],
    ["dark", "Next: System"],
  ] as const)("names the theme it would switch to from %s", async (theme, hint) => {
    renderPalette({ theme });
    useUiStore.getState().setCommandPaletteOpen(true);

    const item = (await screen.findByText("Change theme")).closest(".command-palette-item");
    expect(item).toHaveTextContent(hint);
  });

  it("runs the toggle once and closes the palette", async () => {
    const props = renderPalette({ theme: "system" });
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.click(await screen.findByText("Change theme"));

    expect(props.onToggleTheme).toHaveBeenCalledOnce();
    expect(screen.queryByPlaceholderText("Type a command or search chats…")).not.toBeInTheDocument();
  });

  it.each(["dark", "light", "system", "appearance"])("is found by searching for %s", async (word) => {
    renderPalette();
    useUiStore.getState().setCommandPaletteOpen(true);
    const user = userEvent.setup();

    await user.type(await screen.findByPlaceholderText("Type a command or search chats…"), word);

    expect(await screen.findByText("Change theme")).toBeVisible();
    expect(screen.queryByText("Open settings")).not.toBeInTheDocument();
  });
});
