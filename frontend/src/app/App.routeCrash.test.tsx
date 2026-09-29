import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { readLastCrash } from "../lib/crashLog";
import { ToastProvider } from "./ToastProvider";

// The Settings route crashes as soon as it renders. Everything else is real.
vi.mock("../features/settings/SettingsPanel", () => ({
  SettingsPanel: () => {
    throw new Error("synthetic settings crash");
  },
}));

const WAIT = { timeout: 10_000 };
const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const chats = [
  { id: "thread-a", title: "First chat", timestamp: "2026-07-21T18:00:00Z" },
  { id: "thread-b", title: "Second chat", timestamp: "2026-07-21T17:00:00Z" },
];

function backend() {
  return vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith("/system")) return respond({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond(chats);
    const thread = chats.find((chat) => url.endsWith(`/chats/${chat.id}`));
    if (thread) return respond({ ...thread, revision: 1, messages: [{ id: `m-${thread.id}`, role: "assistant", content: `Transcript of ${thread.title}` }] });
    if (url.endsWith("/settings")) return respond({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/memories")) return respond({ memos: [] });
    if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
    return respond({ detail: "Unexpected test route." }, 404);
  });
}

describe("a crash inside one route", () => {
  beforeAll(async () => {
    // Load the lazy chat route once up front, as App.test.tsx does.
    await import("../features/chat/ChatPage");
  }, 120_000);

  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("leaves the sidebar usable when the settings route crashes", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/thread-a");
    render(<ToastProvider><App api={new CortexApi("/api/v1", backend())} /></ToastProvider>);
    expect(await screen.findByText("Transcript of First chat", {}, WAIT)).toBeVisible();
    const user = userEvent.setup();

    await user.click(screen.getByRole("link", { name: "Settings" }));

    // The route says what happened, and the crash is on record for this session.
    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent("Settings hit a problem");
    expect(readLastCrash()).toMatchObject({ scope: "settings", name: "Error" });
    expect(JSON.stringify(readLastCrash())).not.toContain("synthetic settings crash");
    // Not the whole-window restart page: the shell around it never went away.
    expect(screen.queryByRole("heading", { name: "Cortex needs a restart" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New thread" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Second chat" })).toBeVisible();

    // And the way out works: choosing a chat leaves the crashed route behind.
    await user.click(screen.getByRole("button", { name: "Second chat" }));
    expect(await screen.findByText("Transcript of Second chat", {}, WAIT)).toBeVisible();
    expect(screen.queryByText("Settings hit a problem")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/chat/thread-b");
  });

  it("stays on the route's own error when Try again meets the same fault", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/settings");
    render(<ToastProvider><App api={new CortexApi("/api/v1", backend())} /></ToastProvider>);
    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent("Settings hit a problem");
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent("Settings hit a problem");
    expect(screen.getByRole("button", { name: "New thread" })).toBeVisible();
  });
});
