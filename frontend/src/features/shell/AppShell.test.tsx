import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatGroup, ChatSummary, ModelResponse } from "../../../../contracts/cortex-api";
import { useChatStore } from "../../stores/useChatStore";
import { AppShell } from "./AppShell";

const CONNECTED = { success: true, status: "connected", message: "Connected." } satisfies NonNullable<ModelResponse["connection"]>;

/** Defaults for every prop, so adding one to AppShell does not require
 *  editing each render site here. Override only what the test is about. */
function renderShell(overrides: Partial<React.ComponentProps<typeof AppShell>> = {}) {
  const props: React.ComponentProps<typeof AppShell> = {
    chats: [],
    groups: [] as ChatGroup[],
    activeChatId: null,
    modelConnection: CONNECTED,
    theme: "dark",
    onOpenSettings: vi.fn(),
    onRenameChat: vi.fn<(id: string, title: string) => Promise<void>>().mockResolvedValue(),
    onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(),
    onCreateGroup: vi.fn<(name: string) => Promise<void>>().mockResolvedValue(),
    onRenameGroup: vi.fn<(id: string, name: string) => Promise<void>>().mockResolvedValue(),
    onDeleteGroup: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(),
    onToggleGroup: vi.fn(),
    onMoveChat: vi.fn(),
    children: <div>Chat content</div>,
    ...overrides,
  };
  return { ...render(<AppShell {...props} />), props };
}

describe("AppShell", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("renders a persisted Markdown-wrapped title as plain application text", () => {
    const chat: ChatSummary = { id: "chat-1", title: "**AI Purpose Explained**", timestamp: "2026-01-01T00:00:00Z" };

    renderShell({ chats: [chat], activeChatId: chat.id, onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(), onOpenSettings: vi.fn() });

    expect(screen.getByRole("heading", { name: "AI Purpose Explained" })).toBeVisible();
    expect(screen.getByRole("button", { name: "AI Purpose Explained" })).toBeVisible();
    expect(screen.queryByText("**AI Purpose Explained**")).not.toBeInTheDocument();
    expect(screen.queryByText("Ollama online")).not.toBeInTheDocument();
    expect(screen.queryByText("Connected locally")).not.toBeInTheDocument();
  });

  it("navigates to a new thread without maintaining a second selection state", async () => {
    const user = userEvent.setup();
    const chat: ChatSummary = { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" };

    renderShell({ chats: [chat], activeChatId: chat.id, onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(), onOpenSettings: vi.fn() });

    await user.click(screen.getByRole("button", { name: "New thread" }));

    expect(window.location.pathname).toBe("/chat/new");
  });

  it("captures the routed thread before opening settings", async () => {
    const user = userEvent.setup();
    const chat: ChatSummary = { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" };
    const onOpenSettings = vi.fn();

    renderShell({ chats: [chat], activeChatId: chat.id, onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(), onOpenSettings: onOpenSettings });

    await user.click(screen.getByRole("link", { name: "Settings" }));

    expect(onOpenSettings).toHaveBeenCalledOnce();
    expect(window.location.pathname).toBe("/settings");
  });

  it("requires the exact chat title before permanent deletion", async () => {
    const user = userEvent.setup();
    const chat: ChatSummary = { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" };
    const onDeleteChat = vi.fn<(id: string) => Promise<void>>().mockResolvedValue();

    renderShell({ chats: [chat], activeChatId: chat.id, onDeleteChat: onDeleteChat, onOpenSettings: vi.fn() });

    await user.click(screen.getByRole("button", { name: "Delete Quarterly planning" }));

    expect(screen.getByRole("alertdialog")).toHaveTextContent("Deleted chats cannot be recovered.");
    const confirm = screen.getByRole("button", { name: "Delete permanently" });
    const verifier = screen.getByRole("textbox", { name: /Quarterly planning/ });
    expect(confirm).toBeDisabled();

    await user.type(verifier, "Quarterly plan");
    expect(confirm).toBeDisabled();
    await user.type(verifier, "ning");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    expect(onDeleteChat).toHaveBeenCalledWith(chat.id);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("filters the thread list by title as the user types a search query", async () => {
    const user = userEvent.setup();
    const chats: ChatSummary[] = [
      { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" },
      { id: "chat-2", title: "Recipe ideas", timestamp: "2026-01-01T00:01:00Z" },
    ];

    renderShell({ chats: chats, activeChatId: null, onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(), onOpenSettings: vi.fn() });

    expect(screen.getByRole("button", { name: "Quarterly planning" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recipe ideas" })).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search chats by title"), "recipe");

    expect(screen.queryByRole("button", { name: "Quarterly planning" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recipe ideas" })).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Search chats by title"));
    await user.type(screen.getByLabelText("Search chats by title"), "nothing matches this");

    expect(screen.getByText("No chats match your search.")).toBeInTheDocument();
  });

  it("shows an export control in the header once a full chat is the active chat in the store", () => {
    useChatStore.getState().setActiveChat({
      id: "chat-1",
      title: "Quarterly planning",
      timestamp: "2026-01-01T00:00:00Z",
      revision: 1,
      messages: [{ id: "m-1", role: "user", content: "hi" }],
    });
    const chat: ChatSummary = { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" };

    renderShell({ chats: [chat], activeChatId: chat.id, onDeleteChat: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(), onOpenSettings: vi.fn() });

    expect(screen.getByRole("button", { name: "Export as Markdown" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export as JSON" })).toBeInTheDocument();
  });
});
