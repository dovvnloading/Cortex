import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { useUiStore } from "../stores/useUiStore";
import { ToastProvider } from "./ToastProvider";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const CHATS = [
  { id: "chat-a", title: "Alpha plan", timestamp: "2026-01-02T00:00:00Z" },
  { id: "chat-b", title: "Beta notes", timestamp: "2026-01-01T00:00:00Z" },
];

/** The undo window in App.tsx. */
const UNDO_WINDOW_MS = 6000;

describe("App chat deletion with Undo", () => {
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    vi.useRealTimers();
    act(() => { useUiStore.setState({ toasts: [] }); });
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  function workspace(options: { deleteStatus?: number } = {}) {
    const deletes: string[] = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const chatMatch = /\/chats\/([^/?]+)$/.exec(url);
      if (chatMatch && method === "DELETE") {
        deletes.push(decodeURIComponent(chatMatch[1]));
        return options.deleteStatus && options.deleteStatus >= 400
          ? json({ detail: "The chat could not be deleted." }, options.deleteStatus)
          : new Response(null, { status: 204 });
      }
      if (chatMatch && method === "GET") {
        const chat = CHATS.find((candidate) => candidate.id === chatMatch[1]);
        return chat ? json({ ...chat, revision: 1, messages: [] }) : json({ detail: "Not found." }, 404);
      }
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json(CHATS);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });
    return { fetcher, deletes };
  }

  async function openWorkspace(fetcher: typeof fetch, path = "/chat/new") {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", path);
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("button", { name: "Delete Alpha plan" }, { timeout: 10_000 })).toBeInTheDocument();
  }

  /** Confirm the dialog, then switch to fake timers so the undo window can be stepped through. */
  async function deleteChatAndControlTime(title: string) {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: `Delete ${title}` }));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete chat" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  }

  const passTime = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  /** Let a request the last step started finish. Testing Library's waitFor cannot poll under fake timers. */
  const settle = () => passTime(50);
  const toastRegion = () => within(document.querySelector(".toast-region") as HTMLElement);

  it("hides the chat at once, sends nothing while Undo is offered, and restores it on Undo", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher);

    window.localStorage.setItem("cortex.composer.draft.chat-a", "unsent text, kept for Undo");

    await deleteChatAndControlTime("Alpha plan");

    expect(screen.queryByRole("button", { name: "Delete Alpha plan" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Beta notes" })).toBeInTheDocument();
    expect(toastRegion().getByText("Chat deleted.")).toBeVisible();
    expect(deletes).toEqual([]);

    fireEvent.click(toastRegion().getByRole("button", { name: "Undo" }));
    await passTime(0);

    expect(screen.getByRole("button", { name: "Delete Alpha plan" })).toBeInTheDocument();
    expect(toastRegion().getByText("Chat restored.")).toBeVisible();
    expect(toastRegion().queryByText("Chat deleted.")).not.toBeInTheDocument();

    // Long after the window would have closed, nothing was ever sent, and the draft is intact.
    await passTime(60_000);
    await settle();
    expect(deletes).toEqual([]);
    expect(window.localStorage.getItem("cortex.composer.draft.chat-a")).toBe("unsent text, kept for Undo");
    expect(useChatStore.getState().chats.map((chat) => chat.id)).toEqual(["chat-a", "chat-b"]);
  });

  it("sends exactly one delete when the Undo window closes, and the chat stays gone", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher);
    window.localStorage.setItem("cortex.composer.draft.chat-a", "unsent text for the doomed chat");

    await deleteChatAndControlTime("Alpha plan");
    await passTime(UNDO_WINDOW_MS - 1);
    expect(deletes).toEqual([]);
    expect(toastRegion().getByRole("button", { name: "Undo" })).toBeInTheDocument();

    await passTime(1);
    await settle();
    expect(deletes).toEqual(["chat-a"]);
    await passTime(60_000);

    expect(deletes).toEqual(["chat-a"]);
    expect(window.localStorage.getItem("cortex.composer.draft.chat-a")).toBeNull();
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete Alpha plan" })).not.toBeInTheDocument();
    expect(useChatStore.getState().chats.map((chat) => chat.id)).toEqual(["chat-b"]);
  });

  it("brings the chat back and says so when the delete fails at the end of the window", async () => {
    const { fetcher, deletes } = workspace({ deleteStatus: 500 });
    await openWorkspace(fetcher);

    await deleteChatAndControlTime("Alpha plan");
    await passTime(UNDO_WINDOW_MS);
    await settle();

    expect(deletes).toEqual(["chat-a"]);
    expect(screen.getByRole("button", { name: "Delete Alpha plan" })).toBeInTheDocument();
    expect(toastRegion().getByRole("alert")).toHaveTextContent("The chat could not be deleted.");
    expect(useChatStore.getState().chats.map((chat) => chat.id)).toEqual(["chat-a", "chat-b"]);
  });

  it("keeps a chat hidden while its Undo window is open even if the list is refilled from the server", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher);

    await deleteChatAndControlTime("Alpha plan");
    act(() => { useChatStore.getState().setChats([...CHATS]); });

    expect(screen.queryByRole("button", { name: "Delete Alpha plan" })).not.toBeInTheDocument();
    expect(deletes).toEqual([]);
  });

  it("moves off an open chat when it is deleted and returns to it on Undo", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher, "/chat/chat-a");
    expect(window.location.pathname).toBe("/chat/chat-a");

    await deleteChatAndControlTime("Alpha plan");
    expect(window.location.pathname).toBe("/chat/chat-b");

    fireEvent.click(toastRegion().getByRole("button", { name: "Undo" }));
    await passTime(0);

    expect(window.location.pathname).toBe("/chat/chat-a");
    expect(deletes).toEqual([]);
  });

  it("does not pull the person back to a restored chat after they moved on", async () => {
    const { fetcher } = workspace();
    await openWorkspace(fetcher, "/chat/chat-a");

    await deleteChatAndControlTime("Alpha plan");
    expect(window.location.pathname).toBe("/chat/chat-b");
    act(() => { window.history.pushState({}, "", "/chat/new"); window.dispatchEvent(new PopStateEvent("popstate")); });

    fireEvent.click(toastRegion().getByRole("button", { name: "Undo" }));
    await passTime(0);

    expect(window.location.pathname).toBe("/chat/new");
    expect(screen.getByRole("button", { name: "Delete Alpha plan" })).toBeInTheDocument();
  });
});
