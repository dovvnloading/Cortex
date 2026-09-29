import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useModelStore } from "../stores/useModelStore";
import { ToastProvider } from "./ToastProvider";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const UNAVAILABLE_TEXT = "The selected local model is unavailable";

describe("App model inventory loading", () => {
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    useModelStore.getState().setModels(null);
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  /** A workspace whose model inventory answers only when the test says so. */
  function workspaceWithSlowInventory() {
    let answer: (response: Response) => void = () => undefined;
    const inventory = new Promise<Response>((resolve) => { answer = resolve; });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "qwen3:8b", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return inventory;
      return json({ detail: "Unexpected test route." }, 404);
    });
    return { fetcher, answer };
  }

  const openWorkspace = async (fetcher: typeof fetch) => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" }, { timeout: 10_000 })).toBeVisible();
  };

  const indicator = () => document.querySelector(".connection-indicator") as HTMLElement;

  it("does not report the selected model as unavailable while the inventory is loading", async () => {
    const { fetcher, answer } = workspaceWithSlowInventory();
    await openWorkspace(fetcher);

    // The workspace is open and the inventory has not answered: neutral, not a fault.
    expect(await screen.findByText("Checking local models…")).toBeVisible();
    expect(screen.queryByText(new RegExp(UNAVAILABLE_TEXT))).not.toBeInTheDocument();
    expect(indicator()).toHaveClass("connection-pending");
    expect(indicator()).not.toHaveClass("connection-error");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();

    await act(async () => {
      answer(json({ required_models: [], optional_models: [], installed_models: ["qwen3:8b"], models: [{ name: "qwen3:8b" }], connection: { success: true, status: "connected", message: "Ready" } }));
    });

    await waitFor(() => expect(screen.queryByText("Checking local models…")).not.toBeInTheDocument());
    expect(screen.queryByText(new RegExp(UNAVAILABLE_TEXT))).not.toBeInTheDocument();
    expect(indicator()).toHaveClass("connection-connected");
  });

  it("says the selected model is unavailable once the inventory has answered without it", async () => {
    const { fetcher, answer } = workspaceWithSlowInventory();
    await openWorkspace(fetcher);
    expect(await screen.findByText("Checking local models…")).toBeVisible();

    await act(async () => {
      answer(json({ required_models: [], optional_models: [], installed_models: ["other:1b"], models: [{ name: "other:1b" }], connection: { success: true, status: "connected", message: "Ready" } }));
    });

    expect(await screen.findByText(new RegExp(UNAVAILABLE_TEXT))).toBeVisible();
    expect(screen.queryByText("Checking local models…")).not.toBeInTheDocument();
  });

  it("reports the model service as unavailable when the inventory request fails", async () => {
    const { fetcher, answer } = workspaceWithSlowInventory();
    await openWorkspace(fetcher);
    expect(await screen.findByText("Checking local models…")).toBeVisible();

    await act(async () => { answer(json({ detail: "Model inventory is unavailable." }, 503)); });

    expect(await screen.findByText(new RegExp(UNAVAILABLE_TEXT))).toBeVisible();
    expect(indicator()).toHaveClass("connection-error");
    expect(screen.queryByText("Checking local models…")).not.toBeInTheDocument();
  });
});
