import { render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { ToastProvider } from "./ToastProvider";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

describe("App composer drafts", () => {
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("prunes drafts of chats that no longer exist once the workspace loads, and keeps the rest", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.localStorage.setItem("cortex.composer.draft.chat-a", "still here");
    window.localStorage.setItem("cortex.composer.draft.deleted-chat", "belonged to a deleted chat");
    window.localStorage.setItem("cortex.composer.draft.new", "the new-chat draft");
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([{ id: "chat-a", title: "Alpha plan", timestamp: "2026-01-02T00:00:00Z" }]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("button", { name: "Delete Alpha plan" }, { timeout: 10_000 })).toBeInTheDocument();

    expect(window.localStorage.getItem("cortex.composer.draft.deleted-chat")).toBeNull();
    expect(window.localStorage.getItem("cortex.composer.draft.chat-a")).toBe("still here");
    expect(window.localStorage.getItem("cortex.composer.draft.new")).toBe("the new-chat draft");
  });

  it("does not prune anything when the chat list could not be loaded", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.localStorage.setItem("cortex.composer.draft.chat-a", "must survive a failed load");
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/chats")) return json({ detail: "The chat list is unavailable." }, 500);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "Workspace unavailable" }, { timeout: 10_000 })).toBeVisible();

    expect(window.localStorage.getItem("cortex.composer.draft.chat-a")).toBe("must survive a failed load");
  });
});
