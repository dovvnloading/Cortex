import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useUiStore } from "../stores/useUiStore";
import { ToastProvider } from "./ToastProvider";

/**
 * The command palette searches every chat, and a chat that was just deleted is
 * hidden for the length of its Undo window without having been removed from the
 * backend yet. The palette must agree with the sidebar about which chats exist.
 */

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

describe("command palette with a chat pending deletion", () => {
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    vi.useRealTimers();
    act(() => { useUiStore.setState({ toasts: [], commandPaletteOpen: false }); });
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  function workspace() {
    const deletes: string[] = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const chatMatch = /\/chats\/([^/?]+)$/.exec(url);
      if (chatMatch && method === "DELETE") {
        deletes.push(decodeURIComponent(chatMatch[1]));
        return new Response(null, { status: 204 });
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

  async function openWorkspace(fetcher: typeof fetch) {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("button", { name: "Delete Alpha plan" }, { timeout: 10_000 })).toBeInTheDocument();
  }

  /** Confirm the dialog, then switch to fake timers so the undo window can be stepped through. */
  async function deleteAlphaAndControlTime() {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Delete Alpha plan" }));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete chat" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  }

  const passTime = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  const toastRegion = () => within(document.querySelector(".toast-region") as HTMLElement);

  /** The palette's own rows: the sidebar lists the same titles outside the dialog. */
  const palette = () => within(screen.getByRole("dialog", { name: "Command palette" }));

  async function openPalette() {
    act(() => useUiStore.getState().setCommandPaletteOpen(true));
    await passTime(0);
    return palette();
  }

  async function searchPalette(text: string) {
    fireEvent.change(palette().getByPlaceholderText(/search chats/i), { target: { value: text } });
    // Past the palette's typing pause, so the chat search has caught up.
    await passTime(200);
  }

  const closePalette = () => act(() => useUiStore.getState().setCommandPaletteOpen(false));

  it("leaves the hidden chat out of the recent chats and out of search, and offers it again after Undo", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher);

    await deleteAlphaAndControlTime();
    expect(toastRegion().getByRole("button", { name: "Undo" })).toBeInTheDocument();

    const recent = await openPalette();
    expect(recent.getByText("Beta notes")).toBeInTheDocument();
    expect(recent.queryByText("Alpha plan")).not.toBeInTheDocument();

    await searchPalette("Alpha");
    expect(palette().queryByText("Alpha plan")).not.toBeInTheDocument();
    expect(palette().getByText("No results.")).toBeInTheDocument();
    closePalette();
    await passTime(0);

    fireEvent.click(toastRegion().getByRole("button", { name: "Undo" }));
    await passTime(0);

    const restored = await openPalette();
    expect(restored.getByText("Alpha plan")).toBeInTheDocument();
    expect(restored.getByText("Beta notes")).toBeInTheDocument();
    await searchPalette("Alpha");
    expect(palette().getByText("Alpha plan")).toBeInTheDocument();
    expect(palette().queryByText("Beta notes")).not.toBeInTheDocument();
    expect(deletes).toEqual([]);
  });

  it("still leaves the chat out once the Undo window has closed and the delete was sent", async () => {
    const { fetcher, deletes } = workspace();
    await openWorkspace(fetcher);

    await deleteAlphaAndControlTime();
    await passTime(UNDO_WINDOW_MS);
    await passTime(50);
    expect(deletes).toEqual(["chat-a"]);

    const recent = await openPalette();
    expect(recent.getByText("Beta notes")).toBeInTheDocument();
    expect(recent.queryByText("Alpha plan")).not.toBeInTheDocument();
    await searchPalette("Alpha");
    expect(palette().queryByText("Alpha plan")).not.toBeInTheDocument();
  });
});
