import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ChatSummary, ModelResponse } from "../../../contracts/cortex-api";
import { AppShell } from "./AppShell";

describe("AppShell", () => {
  it("renders a persisted Markdown-wrapped title as plain application text", () => {
    const chat: ChatSummary = { id: "chat-1", title: "**AI Purpose Explained**", timestamp: "2026-01-01T00:00:00Z" };

    render(
      <BrowserRouter>
        <AppShell
          chats={[chat]}
          activeChatId={chat.id}
          modelConnection={{ success: true, status: "connected", message: "Connected." } satisfies NonNullable<ModelResponse["connection"]>}
          theme="dark"
          onSelectChat={vi.fn()}
          onRenameChat={vi.fn<(id: string, title: string) => Promise<void>>().mockResolvedValue()}
          onDeleteChat={vi.fn<(id: string) => Promise<void>>().mockResolvedValue()}
        >
          <div>Chat content</div>
        </AppShell>
      </BrowserRouter>,
    );

    expect(screen.getByRole("heading", { name: "AI Purpose Explained" })).toBeVisible();
    expect(screen.getByRole("button", { name: "AI Purpose Explained" })).toBeVisible();
    expect(screen.queryByText("**AI Purpose Explained**")).not.toBeInTheDocument();
  });

  it("requires the exact chat title before permanent deletion", async () => {
    const user = userEvent.setup();
    const chat: ChatSummary = { id: "chat-1", title: "Quarterly planning", timestamp: "2026-01-01T00:00:00Z" };
    const onDeleteChat = vi.fn<(id: string) => Promise<void>>().mockResolvedValue();

    render(
      <BrowserRouter>
        <AppShell
          chats={[chat]}
          activeChatId={chat.id}
          modelConnection={{ success: true, status: "connected", message: "Connected." } satisfies NonNullable<ModelResponse["connection"]>}
          theme="dark"
          onSelectChat={vi.fn()}
          onRenameChat={vi.fn<(id: string, title: string) => Promise<void>>().mockResolvedValue()}
          onDeleteChat={onDeleteChat}
        >
          <div>Chat content</div>
        </AppShell>
      </BrowserRouter>,
    );

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
});
