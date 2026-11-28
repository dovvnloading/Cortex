import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { ToastProvider } from "./ToastProvider";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const EXISTING = { id: "group-old", name: "Existing folder", collapsed: false, position: 0, timestamp: "2026-01-01T00:00:00Z" };
const CREATED = { id: "group-new", name: "Fresh folder", collapsed: false, position: 1, timestamp: "2026-01-03T00:00:00Z" };

describe("App group list loading", () => {
  beforeAll(async () => {
    await import("../features/chat/ChatPage");
  }, 120_000);

  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("keeps the existing groups when one is created before the first list arrives (#711)", async () => {
    let releaseFirstList!: () => void;
    let listCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/chat-groups") && method === "POST") return json(CREATED, 201);
      if (url.endsWith("/chat-groups")) {
        listCalls += 1;
        if (listCalls === 1) {
          // The first list is slow and was asked for before the create.
          await new Promise<void>((done) => { releaseFirstList = done; });
          return json([EXISTING]);
        }
        return json([EXISTING, CREATED]);
      }
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chats")) return json([{ id: "chat-a", title: "Alpha plan", timestamp: "2026-01-02T00:00:00Z", group_id: "group-old" }]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "New group" }, { timeout: 10_000 }));
    await user.type(screen.getByLabelText("Group name"), "Fresh folder");
    await user.click(screen.getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Rename group Fresh folder" })).toBeInTheDocument());

    releaseFirstList();

    expect(await screen.findByRole("button", { name: "Rename group Existing folder" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rename group Fresh folder" })).toBeInTheDocument();
  });
});
